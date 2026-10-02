"""Unit tests for the P5/P6 trigger path: run_job_once (the job body —
executed IN the sandbox since P6), the job engine itself (agent/jobs.JobRunner
— §4.1 job bodies, dedup, catch-up), and the cron sync (agent/cron_sync.py).

Everything is injected — no Telegram, no Apple, no ~/.herr-claw, no nemoclaw
subprocess. These prove: fixed job names only, once-per-day dedup, honest
job degradation, and idempotent cron registration.
"""

import json
from dataclasses import replace
from datetime import date, datetime, time

import pytest

from agent.cron_sync import (
    CRON_JOB_PREFIX,
    SANDBOX_BRIDGE_URL,
    SCHEDULE_TZ,
    TELEGRAM_TOKEN_PLACEHOLDER,
    _cron_expr,
    _cron_jobs_from_list,
    install_cron,
    trigger_job,
)
from agent.jobs import DEFAULT_JOBS, JobRunner, load_schedule, run_job_once
from agent.bridge import BridgeError as AgentBridgeError
from agent.bridge import BridgeUnreachable as AgentBridgeUnreachable
from agent.state import SessionState, VocabEntry
from herrclaw_bridge.errors import BridgeError


class FakeBridge:
    """call_tool-compatible fake recording every call."""

    def __init__(self, events=None, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.events = events if events is not None else []
        self.fail = fail

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self.fail:
            raise BridgeError("Bridge nicht erreichbar")
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


NOW = datetime(2026, 10, 2, 9, 0)  # Friday, 09:00 — nudge is due


def make_runner(config, *, now=NOW, bridge=None, telegram=None, vault=None,
                jobs=None, session=None, memory=None, srs=None, rng=None):
    """A JobRunner with everything injected (the former daemon test harness)."""
    from agent.state import SrsState

    return JobRunner(
        config,
        srs=srs or SrsState(config.srs_path),
        session=session or SessionState.load(config.session_path),
        telegram=telegram or FakeTelegram(),
        chat_id="111",
        bridge=bridge,
        vault=vault,
        jobs=jobs or dict(DEFAULT_JOBS),
        memory=memory,
        rng=rng,
        now_fn=lambda: now,
        output_fn=lambda line: None,
    )


# -- schedule loading ---------------------------------------------------------


def test_load_schedule_parses_yaml(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text('jobs:\n  nudge: "07:30"\n  quiz: "12:00"\n', encoding="utf-8")
    assert load_schedule(path) == {"nudge": time(7, 30), "quiz": time(12, 0)}


def test_load_schedule_falls_back_to_spec_times(tmp_path, capsys):
    assert load_schedule(tmp_path / "missing.yaml") == DEFAULT_JOBS
    broken = tmp_path / "broken.yaml"
    broken.write_text("jobs: [unclosed", encoding="utf-8")
    assert load_schedule(broken) == DEFAULT_JOBS  # never breaks the trigger path
    assert load_schedule(broken, output_fn=lambda s: None) == DEFAULT_JOBS


def test_repo_schedule_yaml_has_the_spec_jobs():
    jobs = load_schedule()  # agent/schedule.yaml in the repo
    assert jobs == {"nudge": time(8, 0), "quiz": time(12, 30), "recap": time(20, 0)}


# -- pending / dedup / catch-up -------------------------------------------------


def test_pending_and_catchup(config):
    runner = make_runner(config, now=NOW)
    assert runner.pending_jobs(NOW) == ["nudge"]
    assert runner.run("nudge", NOW) is True
    assert runner.pending_jobs(NOW) == []  # marked done — re-fires won't repeat

    session = SessionState.load(config.session_path)
    assert session.jobs_done["nudge"] == "2026-10-02"


def test_failed_job_is_not_marked_done(config):
    runner = make_runner(config, now=NOW, bridge=FakeBridge(fail=True))

    def exploding_send(text):
        raise RuntimeError("no phone")

    runner._send = exploding_send  # nudge fails at the send step
    assert runner.run("nudge", NOW) is False
    assert runner.session.jobs_done.get("nudge") != "2026-10-02"  # retried later


def test_paused_day_skips_jobs_without_marking_failure(config):
    runner = make_runner(config, now=NOW)
    runner.session.pause_until = date(2026, 10, 5)
    assert runner.run("nudge", NOW) is True
    assert runner.session.jobs_done.get("nudge") == "2026-10-02"
    assert runner.telegram.sent == []


# -- 08:00 nudge ------------------------------------------------------------------


def test_nudge_derives_topic_books_slot_and_pings_phone(config):
    events = [
        {"title": "Anmeldung Bürgeramt", "start": "2026-10-02T16:00", "end": "2026-10-02T16:30", "all_day": False}
    ]
    bridge = FakeBridge(events=events)
    runner = make_runner(config, now=NOW, bridge=bridge)
    runner.job_nudge(NOW)

    text = runner.telegram.sent[0]
    assert "Guten Morgen" in text and "Behörden und Termine" in text
    booking_calls = [name for name, _ in bridge.calls]
    assert booking_calls == ["calendar_freebusy", "reminders_add", "calendar_add"]
    reminder_args = bridge.calls[1][1]
    assert reminder_args["title"] == "Deutsch: 5-Min Chat — Thema Behörden und Termine"
    assert reminder_args["due"] == "2026-10-02T09:15"  # first grid slot ≥ now+lead
    assert bridge.calls[2][1]["title"] == "Deutsch Lernen (15m)"


def test_nudge_without_bridge_still_sends(config):
    runner = make_runner(config, now=NOW, bridge=None)
    runner.job_nudge(NOW)
    text = runner.telegram.sent[0]
    assert "Guten Morgen" in text and "Keine Bridge" in text


def test_nudge_degrades_honestly_when_bridge_down(config):
    runner = make_runner(config, now=NOW, bridge=FakeBridge(fail=True))
    runner.job_nudge(NOW)
    text = runner.telegram.sent[0]
    assert "Guten Morgen" in text  # the nudge still goes out
    assert "Keine Kalenderprüfung" in text or "fehlgeschlagen" in text


def test_nudge_falls_back_to_weakest_vocab_theme(config):
    runner = make_runner(config, now=NOW, bridge=FakeBridge(events=[]))
    from agent.state import SrsState

    srs = SrsState(config.srs_path)
    srs.vocab["der Zug"] = VocabEntry(en="train", level=1)  # theme: Berlin & Alltag
    runner.srs = srs
    runner.ctx.srs = srs
    runner.job_nudge(NOW)
    assert "Berlin & Alltag" in runner.telegram.sent[0]


# -- 12:30 micro-quiz ----------------------------------------------------------------


def test_quiz_job_only_runs_when_nudge_ran(config):
    runner = make_runner(config, now=datetime(2026, 10, 2, 12, 30))
    runner.job_quiz(runner._now())
    assert runner.telegram.sent == []  # morning nudge missing → no quiz

    runner.session.jobs_done["nudge"] = "2026-10-02"
    runner.job_quiz(runner._now())
    assert len(runner.telegram.sent) == 1
    assert "Mini-Quiz" in runner.telegram.sent[0]
    assert "Frage 1/3" in runner.telegram.sent[0]
    assert runner.ctx.quiz is not None and len(runner.ctx.quiz.questions) == 3


def test_quiz_job_does_not_stack_a_second_quiz(config):
    runner = make_runner(config, now=datetime(2026, 10, 2, 12, 30))
    runner.session.jobs_done["nudge"] = "2026-10-02"
    runner.job_quiz(runner._now())
    active = runner.ctx.quiz
    runner.job_quiz(runner._now())  # quiz still running → ignored
    assert runner.ctx.quiz is active


# -- 20:00 recap -----------------------------------------------------------------------


def test_recap_writes_daily_note_progress_and_completes_reminders(config, vault):
    bridge = FakeBridge()
    runner = make_runner(config, now=datetime(2026, 10, 2, 20, 0), bridge=bridge, vault=vault)
    runner.srs.touch_day(date(2026, 10, 2))
    runner.job_recap(runner._now())

    assert ("reminders_complete", {"title": "5-Min Chat"}) in bridge.calls
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "Tages-Recap" in daily and "Streak: 1" in daily
    progress = vault.read("Deutsch/progress.md")
    assert "Tages-Recap" in progress
    assert "Tages-Recap gespeichert" in runner.telegram.sent[0]


def test_recap_without_vault_still_confirms(config):
    runner = make_runner(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(), vault=None)
    runner.job_recap(runner._now())
    assert runner.telegram.sent and "Tages-Recap" in runner.telegram.sent[0]


def test_recap_bridge_down_still_writes_note(config, vault):
    runner = make_runner(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(fail=True), vault=vault)
    runner.srs.touch_day(date(2026, 10, 2))
    runner.job_recap(runner._now())
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "nicht abgehakt" in daily  # honest degradation is visible in the note


# -- v0.5: Morgen-Brief (08:00 nudge) ----------------------------------------------------


def test_nudge_sends_the_morgen_brief_with_phrases(config, vault):
    from agent.phrases import PHRASE_BANK

    events = [
        {"title": "Anmeldung Bürgeramt", "start": "2026-10-02T16:00", "end": "2026-10-02T16:30", "all_day": False}
    ]
    runner = make_runner(config, now=NOW, bridge=FakeBridge(events=events), vault=vault)
    runner.job_nudge(NOW)

    text = runner.telegram.sent[0]
    assert "Guten Morgen" in text and "Behörden und Termine" in text
    assert "Deine Phrasen für heute:" in text
    assert "1. " in text
    phrases = [line.split(". ", 1)[1] for line in text.splitlines() if line[:2].strip(".").isdigit()]
    assert 5 <= len(phrases) <= 8  # SPEC §4.6: 5–8 phrases
    assert any(p in PHRASE_BANK["Behörden und Termine"] for p in phrases)  # event-tailored
    # the plan is stored so /tag re-shows exactly this all day
    plan = runner.session.day_plan
    assert plan["date"] == "2026-10-02" and plan["source"] == "calendar"
    assert plan["phrases"] == phrases
    # the daily note carries the same brief
    daily = vault.read("Daily notes/2026-10-02-deutsch.md")
    assert "Morgen-Brief" in daily and "Behörden und Termine" in daily


def test_nudge_admits_when_the_calendar_was_unreadable(config):
    runner = make_runner(config, now=NOW, bridge=FakeBridge(fail=True))
    runner.job_nudge(NOW)
    text = runner.telegram.sent[0]
    assert "Kein Kalender gelesen" in text  # honest fallback (SPEC §4.6)
    assert runner.session.day_plan["source"] == "fallback"


def test_nudge_lets_memory_interests_pick_the_flavor(config):
    from agent.memory import MemoryState

    from agent.phrases import PHRASE_BANK

    memory = MemoryState.load(config.memory_path)
    memory.record_interest("Bäckerei", landed=True)
    runner = make_runner(config, now=NOW, bridge=FakeBridge(events=[]), memory=memory)
    runner.job_nudge(NOW)
    text = runner.telegram.sent[0]
    assert any(phrase in text for phrase in PHRASE_BANK["Beim Bäcker"])  # flavor came from memory


# -- v0.5: Abend-Recap Erfolgs-Check -------------------------------------------------------


def test_recap_asks_which_phrases_were_used(config, vault):
    runner = make_runner(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge(), vault=vault)
    runner.session.day_plan = {
        "date": "2026-10-02",
        "topic": "Beim Bäcker",
        "phrases": ["a", "b", "c", "d", "e"],
        "source": "calendar",
    }
    runner.job_recap(runner._now())
    text = runner.telegram.sent[0]
    assert "Tages-Recap gespeichert" in text
    assert "Abend-Check" in text and "/erfolge" in text
    assert "5 heutigen Phrasen" in text


def test_recap_without_plan_still_asks(config):
    runner = make_runner(config, now=datetime(2026, 10, 2, 20, 0), bridge=FakeBridge())
    runner.job_recap(runner._now())
    assert "Abend-Check" in runner.telegram.sent[0]


# -- run_job_once (the job body — executes in the sandbox, P6) -----------------


def test_nudge_runs_in_the_sandbox_and_marks_done(config, vault):
    bridge, telegram = FakeBridge(), FakeTelegram()

    result = run_job_once(
        "nudge",
        cfg=config,
        vault=vault,
        telegram=telegram,
        bridge=bridge,
        now=datetime(2026, 10, 2, 8, 0),
        output_fn=None,
    )

    assert "erledigt" in result
    assert any(n == "calendar_freebusy" for n, _ in bridge.calls)
    assert any(n == "reminders_add" for n, _ in bridge.calls)
    assert telegram.sent and "Guten Morgen" in telegram.sent[0]
    session = json.loads(config.session_path.read_text())
    assert session["jobs_done"]["nudge"] == "2026-10-02"


def test_dedup_second_fire_same_day_is_skipped(config, vault):
    bridge, telegram = FakeBridge(), FakeTelegram()
    once = dict(cfg=config, vault=vault, telegram=telegram, bridge=bridge, output_fn=None)

    assert "erledigt" in run_job_once("nudge", now=datetime(2026, 10, 2, 8, 0), **once)
    again = run_job_once("nudge", now=datetime(2026, 10, 2, 9, 0), **once)

    assert "übersprungen" in again
    assert len(telegram.sent) == 1  # no double-fire


def test_unknown_job_is_refused(config, vault):
    with pytest.raises(BridgeError, match="Unbekannter Job"):
        run_job_once(
            "rm -rf /",
            cfg=config,
            vault=vault,
            telegram=FakeTelegram(),
            bridge=FakeBridge(),
        )


def test_quiz_requires_nudge_first(config, vault):
    telegram = FakeTelegram()
    run_job_once(
        "quiz",
        cfg=config,
        vault=vault,
        telegram=telegram,
        bridge=FakeBridge(),
        now=datetime(2026, 10, 2, 7, 30),  # manual early trigger — nudge not due yet
        output_fn=None,
    )
    assert telegram.sent == []  # nudge never ran today → quiz skipped (no catch-up before 08:00)


def test_quiz_fire_catches_up_missed_nudge(config, vault):
    """The 08:00 cron fire failed (bridge blip)? The 12:30 quiz fire heals the
    day: nudge runs first, THEN the quiz — instead of skipping itself."""
    telegram = FakeTelegram()
    result = run_job_once(
        "quiz",
        cfg=config,
        vault=vault,
        telegram=telegram,
        bridge=FakeBridge(),
        now=datetime(2026, 10, 2, 12, 30),
        output_fn=None,
    )
    assert "Nachgeholt: „nudge“" in result and "erledigt" in result
    assert "Guten Morgen" in telegram.sent[0]  # the morning brief, late but sent
    assert any("Mini-Quiz" in text for text in telegram.sent[1:])
    session = json.loads(config.session_path.read_text())
    assert session["jobs_done"] == {"nudge": "2026-10-02", "quiz": "2026-10-02"}


def test_trigger_catchup_never_doubles_or_runs_future(config, vault):
    telegram = FakeTelegram()
    once = dict(cfg=config, vault=vault, telegram=telegram, bridge=FakeBridge(), output_fn=None)
    run_job_once("nudge", now=datetime(2026, 10, 2, 8, 0), **once)

    result = run_job_once("quiz", now=datetime(2026, 10, 2, 12, 30), **once)

    assert "Nachgeholt" not in result  # nudge already done — no double-fire
    assert sum("Guten Morgen" in text for text in telegram.sent) == 1
    assert not any("Recap" in text or "recap" in text for text in telegram.sent)


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
        watchdogs={},
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
        assert " trigger " in joined  # the cron fires `herr-claw trigger <job>`
        # the cron entries carry the complete sandbox env (P6): state dir +
        # chat id + the Telegram token PLACEHOLDER (never a real value)
        assert "HERR_STATE_DIR=/sandbox/herrclaw/state" in joined
        assert f"TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN_PLACEHOLDER}" in joined
        assert "HERR_TELEGRAM_CHAT_ID=111" not in joined  # config fixture has no chat id
    nudge = next(args for args in adds if "herr-claw-nudge" in args)
    assert nudge[nudge.index("--cron") + 1] == "0 8 * * *"


def test_install_cron_registers_the_telegram_watchdog():
    runner = recording_runner()

    rc = install_cron(
        jobs={"nudge": time(8, 0)},
        watchdogs={"telegram": "*/5 * * * *"},
        runner=runner,
        sandbox="sbx",
        output_fn=lambda line: None,
    )

    assert rc == 0
    adds = [args for args in runner.calls if "add" in args]
    assert len(adds) == 2
    watchdog = next(args for args in adds if "herr-claw-telegram-watchdog" in args)
    assert watchdog[watchdog.index("--cron") + 1] == "*/5 * * * *"
    assert " telegram-watchdog" in " ".join(watchdog)  # keeps the receiver alive


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
        watchdogs={},
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

    assert install_cron(jobs={"nudge": time(8, 0)}, watchdogs={}, runner=run, sandbox="sbx",
                        output_fn=lambda line: None) == 1


def test_trigger_job_executes_the_job_body_locally(config, capsys):
    """P6: the trigger no longer relays to a host-side run_job — the body
    runs right here; only its Apple/vault calls cross the bridge."""
    calls = []

    def job_fn(name, *, cfg, output_fn):
        calls.append((name, cfg))
        output_fn("⏰ Nachgeholt: „nudge“ — erledigt")
        return "Job „quiz“ erledigt."

    rc = trigger_job("quiz", cfg=config, job_fn=job_fn)
    assert rc == 0
    assert calls == [("quiz", config)]
    out = capsys.readouterr().out
    assert "Nachgeholt" in out and "erledigt" in out


def test_trigger_job_reports_bridge_failure(config, capsys):
    def job_fn(name, *, cfg, output_fn):
        raise AgentBridgeError("Bridge nicht erreichbar.")

    assert trigger_job("nudge", cfg=config, job_fn=job_fn) == 1
    assert "Bridge nicht erreichbar" in capsys.readouterr().out


def test_trigger_job_retries_unreachable_then_succeeds(config, capsys):
    attempts = []

    def job_fn(name, *, cfg, output_fn):
        attempts.append(1)
        if len(attempts) < 3:
            raise AgentBridgeUnreachable("Bridge nicht erreichbar (http://bridge.test/mcp).")
        return "Job „nudge“ erledigt."

    sleeps: list[float] = []
    rc = trigger_job("nudge", cfg=config, job_fn=job_fn, backoff_s=5.0, sleep_fn=sleeps.append)
    assert rc == 0 and len(attempts) == 3 and sleeps == [5.0, 5.0]
    assert "erledigt" in capsys.readouterr().out


def test_trigger_job_gives_up_after_attempts(config, capsys):
    attempts = []

    def job_fn(name, *, cfg, output_fn):
        attempts.append(1)
        raise AgentBridgeUnreachable("Bridge nicht erreichbar.")

    sleeps: list[float] = []
    rc = trigger_job("nudge", cfg=config, job_fn=job_fn, attempts=3, backoff_s=1.0,
                     sleep_fn=sleeps.append)
    assert rc == 1 and len(attempts) == 3 and sleeps == [1.0, 1.0]
    assert "Versuch 2/3" in capsys.readouterr().out


def test_trigger_job_never_retries_denial(config, capsys):
    attempts = []

    def job_fn(name, *, cfg, output_fn):
        attempts.append(1)
        raise AgentBridgeError("Unbekannter Job „nudge“ — erlaubt sind: …")

    sleeps: list[float] = []
    rc = trigger_job("nudge", cfg=config, job_fn=job_fn, backoff_s=1.0,
                     sleep_fn=sleeps.append)
    assert rc == 1 and len(attempts) == 1 and sleeps == []  # a denial is final


def test_repo_schedule_yaml_carries_the_watchdog():
    from agent.cron_sync import load_watchdogs

    assert load_watchdogs() == {"telegram": "*/5 * * * *"}


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
