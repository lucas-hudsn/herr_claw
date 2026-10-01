"""Unit tests for the P5 trigger path: run_job_once (the run_job MCP tool's
engine) and the cron sync (agent/cron_sync.py).

Everything is injected — no Telegram, no Apple, no ~/.herr-claw, no nemoclaw
subprocess. These prove: fixed job names only, once-per-day dedup shared
with the daemon loop, audited runs, and idempotent cron registration.
"""

import json
from dataclasses import replace
from datetime import datetime, time

import pytest

from agent.cron_sync import (
    CRON_JOB_PREFIX,
    SANDBOX_BRIDGE_URL,
    SCHEDULE_TZ,
    _cron_expr,
    _cron_jobs_from_list,
    install_cron,
    trigger_job,
)
from agent.jobs import run_job_once
from agent.bridge import BridgeError as AgentBridgeError
from herrclaw_bridge.errors import BridgeError


class FakeBridge:
    """call_tool-compatible fake recording every call."""

    def __init__(self, events=None):
        self.calls: list[tuple[str, dict]] = []
        self.events = events if events is not None else []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "calendar_freebusy":
            return json.dumps({"events": self.events})
        if name == "reminders_add":
            return f"Erinnerung angelegt: {arguments['title']}"
        if name == "calendar_add":
            return f"Termin angelegt: {arguments['title']}"
        if name == "reminders_complete":
            return f"Abgehakt: {arguments['title']}"
        raise AssertionError(f"unerwartetes Tool {name}")


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, chat_id, text):
        self.sent.append(text)


# -- run_job_once -----------------------------------------------------------


def test_nudge_runs_host_side_and_marks_done(config, vault, tmp_path):
    bridge, telegram = FakeBridge(), FakeTelegram()
    audit_log = tmp_path / "audit.log"

    result = run_job_once(
        "nudge",
        cfg=config,
        vault=vault,
        telegram=telegram,
        bridge=bridge,
        audit_log=audit_log,
        now=datetime(2026, 10, 2, 8, 0),
        output_fn=None,
    )

    assert "erledigt" in result
    assert any(n == "calendar_freebusy" for n, _ in bridge.calls)
    assert any(n == "reminders_add" for n, _ in bridge.calls)
    assert telegram.sent and "Guten Morgen" in telegram.sent[0]
    session = json.loads(config.session_path.read_text())
    assert session["jobs_done"]["nudge"] == "2026-10-02"
    entries = [json.loads(line) for line in audit_log.read_text().strip().splitlines()]
    last = entries[-1]
    assert last["action"] == "job.run" and last["target"] == "nudge" and last["allowed"] is True


def test_dedup_second_fire_same_day_is_skipped(config, vault, tmp_path):
    bridge, telegram = FakeBridge(), FakeTelegram()
    once = dict(cfg=config, vault=vault, telegram=telegram, bridge=bridge,
                audit_log=tmp_path / "audit.log", output_fn=None)

    assert "erledigt" in run_job_once("nudge", now=datetime(2026, 10, 2, 8, 0), **once)
    again = run_job_once("nudge", now=datetime(2026, 10, 2, 9, 0), **once)

    assert "übersprungen" in again
    assert len(telegram.sent) == 1  # no double-fire


def test_unknown_job_denied_and_audited(config, vault, tmp_path):
    audit_log = tmp_path / "audit.log"
    with pytest.raises(BridgeError, match="Unbekannter Job"):
        run_job_once(
            "rm -rf /",
            cfg=config,
            vault=vault,
            telegram=FakeTelegram(),
            bridge=FakeBridge(),
            audit_log=audit_log,
        )
    entries = [json.loads(line) for line in audit_log.read_text().strip().splitlines()]
    denied = [e for e in entries if e["action"] == "job.run" and not e["allowed"]]
    assert denied and denied[0]["target"] == "rm -rf /"


def test_quiz_requires_nudge_first(config, vault, tmp_path):
    telegram = FakeTelegram()
    run_job_once(
        "quiz",
        cfg=config,
        vault=vault,
        telegram=telegram,
        bridge=FakeBridge(),
        audit_log=tmp_path / "audit.log",
        now=datetime(2026, 10, 2, 12, 30),
        output_fn=None,
    )
    assert telegram.sent == []  # nudge never ran today → quiz skipped


# -- cron sync ----------------------------------------------------------------


def recording_runner(existing: list[tuple[str, str]] | None = None):
    """Fake nemoclaw runner: answers `cron list --json` from `existing`
    (name, id) pairs, records every other command, always succeeds."""
    calls: list[list[str]] = []

    def run(sandbox, args):
        if args[:3] == ["openclaw", "cron", "list"]:
            payload = json.dumps(
                {"jobs": [{"name": name, "id": job_id} for name, job_id in (existing or [])]}
            )
            return 0, payload
        calls.append(args)
        return 0, ""

    run.calls = calls
    return run


def test_cron_expr_from_schedule_times():
    assert _cron_expr(time(8, 0)) == "0 8 * * *"
    assert _cron_expr(time(12, 30)) == "30 12 * * *"


def test_job_names_tolerant_json_shapes():
    pairs = _cron_jobs_from_list('{"jobs": [{"name": "a", "id": "1"}, {"name": "b", "id": "2"}]}')
    assert pairs == [("a", "1"), ("b", "2")]
    assert _cron_jobs_from_list('[{"name": "a", "id": "1"}]') == [("a", "1")]
    assert _cron_jobs_from_list("kein json") == []
    assert _cron_jobs_from_list('{"jobs": [{"name": "no-id"}]}') == []


def test_install_cron_registers_exact_berlin_jobs():
    runner = recording_runner()

    rc = install_cron(
        jobs={"nudge": time(8, 0), "quiz": time(12, 30)},
        runner=runner,
        sandbox="sbx",
        output_fn=lambda line: None,
    )

    assert rc == 0
    adds = [args for args in runner.calls if "add" in args]
    assert len(adds) == 2
    for args in adds:
        joined = " ".join(args)
        assert SCHEDULE_TZ in joined and "--exact" in joined
        assert SANDBOX_BRIDGE_URL in joined
        assert "daemon --trigger" in joined
    nudge = next(args for args in adds if "herr-claw-nudge" in args)
    assert nudge[nudge.index("--cron") + 1] == "0 8 * * *"


def test_install_cron_replaces_and_removes_drift():
    runner = recording_runner(
        existing=[
            (f"{CRON_JOB_PREFIX}nudge", "id-a"),
            (f"{CRON_JOB_PREFIX}nudge", "id-a2"),  # a duplicate from an older sync
            (f"{CRON_JOB_PREFIX}old", "id-b"),
            ("foreign", "id-c"),
        ]
    )

    rc = install_cron(
        jobs={"nudge": time(8, 0)},
        runner=runner,
        sandbox="sbx",
        output_fn=lambda line: None,
    )

    assert rc == 0
    joined_calls = [" ".join(args) for args in runner.calls]
    rms = [c for c in joined_calls if " rm " in c]
    assert len(rms) == 3  # both nudge ids + the drift job, by id
    assert all("id-a" in c or "id-a2" in c or "id-b" in c for c in rms)
    assert len([c for c in joined_calls if " add " in c]) == 1
    assert not any("foreign" in c for c in joined_calls)  # never touched


def test_install_cron_fails_when_list_fails():
    def run(sandbox, args):
        return 3, "kaputt"

    assert install_cron(jobs={"nudge": time(8, 0)}, runner=run, sandbox="sbx",
                        output_fn=lambda line: None) == 1


def test_trigger_job_calls_bridge_run_job(config, capsys):
    class OkClient:
        def __init__(self):
            self.calls = []

        def call_tool(self, name, arguments=None):
            self.calls.append((name, arguments))
            return "Job „nudge“ erledigt."

    client = OkClient()
    cfg = replace(config, bridge_url="http://bridge.test/mcp")
    rc = trigger_job("nudge", cfg=cfg, client=client)
    assert rc == 0
    assert client.calls == [("run_job", {"name": "nudge"})]
    assert "erledigt" in capsys.readouterr().out


def test_trigger_job_reports_bridge_failure(config, capsys):
    class DownClient:
        def call_tool(self, name, arguments=None):
            raise AgentBridgeError("Bridge nicht erreichbar.")

    cfg = replace(config, bridge_url="http://bridge.test/mcp")
    assert trigger_job("nudge", cfg=cfg, client=DownClient()) == 1
    assert "Bridge nicht erreichbar" in capsys.readouterr().out


# -- policy artifact (P5) -----------------------------------------------------

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def test_policy_additions_pins_bridge_endpoint_only():
    """The committed OpenShell preset must expose exactly the bridge endpoint,
    verb-scoped to /mcp — any wider egress in the file is a regression. The
    trigger venv's wheels are vendored from the host, so no package-registry
    egress is ever added."""
    doc = yaml.safe_load((REPO / "nemoclaw-blueprint" / "policy-additions.yaml").read_text())
    assert doc["preset"]["name"] == "herrclaw-bridge"
    policies = doc["network_policies"]
    assert set(policies) == {"herrclaw-bridge"}
    (endpoint,) = policies["herrclaw-bridge"]["endpoints"]
    assert endpoint["host"] == "host.openshell.internal" and endpoint["port"] == 8765
    assert endpoint["protocol"] == "rest" and endpoint["enforcement"] == "enforce"
    assert {(r["allow"]["method"], r["allow"]["path"]) for r in endpoint["rules"]} == {
        ("POST", "/mcp"),
        ("GET", "/mcp"),
        ("DELETE", "/mcp"),
    }
    assert "binaries" not in policies["herrclaw-bridge"]  # applied via `openshell policy set` — see the file's header


def test_cron_jobs_from_list_tolerates_banner_noise():
    noisy = "✓ Active gateway set to 'nemoclaw'\n{\"jobs\": [{\"name\": \"a\", \"id\": \"1\"}]}\n"
    assert _cron_jobs_from_list(noisy) == [("a", "1")]
