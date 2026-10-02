"""Sandbox cron sync + trigger path (P5, P6) — the ONE scheduler wiring.

The ONE scheduler is the OpenClaw cron inside sandbox `my-assistant`
(SPEC §4.1). This module is the only code that touches it:

- `trigger_job(name)` — the in-sandbox trigger path: the §4.1 job BODY
  executes right here (agent/jobs.run_job_once); its Apple/vault calls
  cross the audited bridge. An unreachable bridge is retried with backoff
  (denials never are).
- `install_cron()` — reads agent/schedule.yaml (still the ONE schedule
  config) and re-registers one cron job per §4.1 job plus the Telegram
  watchdog via `nemoclaw <sandbox> exec -- openclaw cron …`. Re-syncing
  replaces the `herr-claw-*` jobs wholesale (run history resets) and
  removes drifted jobs no longer present in schedule.yaml.

Times in schedule.yaml are Berlin wall clock, so entries pin
`--tz Europe/Berlin` regardless of the sandbox's own timezone. Cron
entries carry their complete environment via --command-env: the bridge
host alias, the sandbox state dir, the chat id, and the Telegram token
PLACEHOLDER (`openshell:resolve:env:` — the gateway resolves it at
egress; the token itself never enters the sandbox).
"""

from __future__ import annotations

import json
import os
import subprocess
import time as _time

from .bridge import BridgeError, BridgeUnreachable
from .config import SANDBOX_STATE_DIR, load_config
from .jobs import load_schedule

SANDBOX_ENV_VAR = "HERR_NEMOCLAW_SANDBOX"
DEFAULT_SANDBOX = "my-assistant"
SCHEDULE_TZ = "Europe/Berlin"
SANDBOX_BRIDGE_URL = "http://host.openshell.internal:8765/mcp"
SANDBOX_APP_DIR = "/sandbox/herrclaw"
SANDBOX_HERR_CLAW = f"{SANDBOX_APP_DIR}/.venv/bin/herr-claw"
CRON_JOB_PREFIX = "herr-claw-"
WATCHDOG_SUFFIX = "-watchdog"
# The gateway resolves this placeholder to the real token at egress — the
# literal string is safe to carry in cron config and logs. It MUST be the
# full gateway-issued credential-binding form (with the `v…_` binding id);
# the short `openshell:resolve:env:TELEGRAM_BOT_TOKEN` form does NOT resolve
# (gateway 500s, the telegram-loop can never start). If the gateway ever
# reissues the binding, re-derive the current form from a live sandbox shell
# (`nemoclaw <sandbox> exec -- env`, TELEGRAM_BOT_TOKEN value), update this
# constant, and re-run `herr-claw install-cron`.
TELEGRAM_TOKEN_PLACEHOLDER = "openshell:resolve:env:v8392926173393239856_TELEGRAM_BOT_TOKEN"
JOB_TIMEOUT_S = 120  # calendar/reminder round-trips can be slow; cron default is 30 s
WATCHDOG_TIMEOUT_S = 60


def sandbox_name() -> str:
    return os.environ.get(SANDBOX_ENV_VAR, "").strip() or DEFAULT_SANDBOX


def trigger_job(
    name: str,
    *,
    cfg=None,
    job_fn=None,
    output_fn=print,
    attempts: int = 3,
    backoff_s: float = 15.0,
    sleep_fn=_time.sleep,
) -> int:
    """One trigger → the job body executes HERE in the sandbox; its
    Apple/vault calls are the only traffic that crosses the bridge (each
    one audited there).

    Transport-level failures (bridge momentarily down — host rebooting,
    `herr-claw bridge` restarting) are retried with backoff: a cron fire
    should survive a blip, not lose the day's job to it. Policy denials and
    unknown-job refusals (plain BridgeError) are never retried — the bridge
    refused the call, so trying again would only spam the audit log."""
    from .jobs import run_job_once

    cfg = cfg or load_config()
    job_fn = job_fn or run_job_once
    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        output_fn(line)

    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = job_fn(name, cfg=cfg, output_fn=collect)
            break  # success — the day is current
        except BridgeUnreachable as exc:
            if attempt == attempts:
                output_fn(f"⚠︎ {exc}")
                return 1
            output_fn(f"⚠︎ {exc} — Versuch {attempt}/{attempts}, neuer Versuch in {backoff_s:.0f} s.")
            sleep_fn(backoff_s)
        except BridgeError as exc:
            output_fn(f"⚠︎ {exc}")
            return 1
    output_fn(result)
    return 0


def _default_runner(sandbox: str, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["nemoclaw", sandbox, "exec", "--", *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _cron_expr(hhmm) -> str:
    return f"{hhmm.minute} {hhmm.hour} * * *"


def load_watchdogs(path=None, output_fn=print) -> dict[str, str]:
    """schedule.yaml `watchdogs:` → {name: raw cron expr}. Missing/empty →
    {} (watchdogs are optional self-healing, never load-bearing)."""
    from pathlib import Path

    path = path or Path(__file__).resolve().parent / "schedule.yaml"
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k): str(v) for k, v in (raw.get("watchdogs") or {}).items()}
    except Exception:  # malformed/missing yaml — watchdogs are optional
        return {}


def _cron_jobs_from_list(output: str) -> list[tuple[str, str]]:
    """(name, id) pairs from `openclaw cron list --json` — cron rm/run take the
    id, not the name. Tolerates nemoclaw's banner noise around the JSON."""
    start = min((i for i in (output.find("{"), output.find("[")) if i != -1), default=-1)
    if start == -1:
        return []
    try:
        data, _end = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("jobs") or data.get("items") or []
    if not isinstance(data, list):
        return []
    return [
        (str(job.get("name", "")), str(job.get("id", "")))
        for job in data
        if isinstance(job, dict) and job.get("name") and job.get("id")
    ]


def _cron_env(output_fn=print) -> list[str]:
    """The complete cron-entry environment: bridge alias, sandbox state dir,
    chat id, Telegram token placeholder (gateway resolves it at egress)."""
    env = [
        f"HERR_BRIDGE_URL={SANDBOX_BRIDGE_URL}",
        f"HERR_STATE_DIR={SANDBOX_STATE_DIR}",
        f"TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN_PLACEHOLDER}",
    ]
    chat_id = load_config().telegram_chat_id
    if chat_id:
        env.append(f"HERR_TELEGRAM_CHAT_ID={chat_id}")
    else:
        output_fn("⚠︎ HERR_TELEGRAM_CHAT_ID ist nicht gesetzt — die Cron-Jobs senden nicht.")
    return env


def install_cron(
    *,
    jobs: dict | None = None,
    watchdogs: dict[str, str] | None = None,
    runner=None,
    sandbox: str = "",
    output_fn=print,
) -> int:
    """Register one OpenClaw cron job per schedule.yaml entry plus the
    watchdogs, idempotently: ALL existing `herr-claw-*` jobs are removed
    (by id — dedupes re-syncs), then every wanted job is added fresh.
    Non-herr-claw jobs are never touched."""
    jobs = jobs if jobs is not None else load_schedule(output_fn=output_fn)
    watchdogs = watchdogs if watchdogs is not None else load_watchdogs(output_fn=output_fn)
    sandbox = sandbox or sandbox_name()
    runner = runner or _default_runner
    env = _cron_env(output_fn)

    rc, out = runner(sandbox, ["openclaw", "cron", "list", "--json"])
    if rc != 0:
        output_fn(f"⚠︎ openclaw cron list fehlgeschlagen in Sandbox „{sandbox}“:\n{out.strip()}")
        return 1
    existing = _cron_jobs_from_list(out)

    wanted: dict[str, tuple[list[str], str]] = {}
    for name, t in jobs.items():
        args = [
            "openclaw", "cron", "add",
            "--name", f"{CRON_JOB_PREFIX}{name}",
            "--cron", _cron_expr(t),
            "--tz", SCHEDULE_TZ,
            "--exact",
            "--no-deliver",  # the job sends Telegram itself; stdout stays in the cron log
            "--description", f"Herr Claw §4.1 {name} (aus agent/schedule.yaml)",
            "--timeout-seconds", str(JOB_TIMEOUT_S),
            "--command", f"{SANDBOX_HERR_CLAW} trigger {name}",
            "--command-cwd", SANDBOX_APP_DIR,
        ]
        for kv in env:
            args += ["--command-env", kv]
        wanted[f"{CRON_JOB_PREFIX}{name}"] = (args, _cron_expr(t))
    for name, expr in watchdogs.items():
        args = [
            "openclaw", "cron", "add",
            "--name", f"{CRON_JOB_PREFIX}{name}{WATCHDOG_SUFFIX}",
            "--cron", expr,
            "--tz", SCHEDULE_TZ,
            "--exact",
            "--no-deliver",
            "--description", f"Herr Claw {name}-Watchdog (Selbstheilung, aus schedule.yaml)",
            "--timeout-seconds", str(WATCHDOG_TIMEOUT_S),
            "--command", f"{SANDBOX_HERR_CLAW} telegram-watchdog",
            "--command-cwd", SANDBOX_APP_DIR,
        ]
        for kv in env:
            args += ["--command-env", kv]
        wanted[f"{CRON_JOB_PREFIX}{name}{WATCHDOG_SUFFIX}"] = (args, expr)

    failed = False
    for job_name, job_id in existing:
        if not job_name.startswith(CRON_JOB_PREFIX):
            continue
        rc, out = runner(sandbox, ["openclaw", "cron", "rm", job_id])
        if rc != 0:
            label = "Drift-Job" if job_name not in wanted else f"Alt-Job „{job_name}“"
            output_fn(f"⚠︎ {label} „{job_name}“ ({job_id}) konnte nicht entfernt werden:\n{out.strip()}")
            failed = True
        else:
            output_fn(f"🗑 „{job_name}“ ({job_id[:8]}) entfernt.")

    for job_name, (args, expr) in sorted(wanted.items(), key=lambda item: item[1][1]):
        rc, out = runner(sandbox, args)
        if rc != 0:
            output_fn(f"⚠︎ Cron-Job „{job_name}“ konnte nicht angelegt werden:\n{out.strip()}")
            failed = True
        else:
            output_fn(f"⏰ „{job_name}“ → {expr} ({SCHEDULE_TZ}) — läuft in der Sandbox.")

    if not failed:
        output_fn(
            f"✓ {len(wanted)} Cron-Job(s) in Sandbox „{sandbox}“ registriert — "
            "der OpenClaw-cron ist der EINE Scheduler."
        )
    return 1 if failed else 0
