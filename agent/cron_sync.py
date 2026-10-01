"""Sandbox cron sync + trigger client (P5) — the ONE scheduler wiring.

The ONE scheduler is the OpenClaw cron inside sandbox `my-assistant`
(SPEC §4.1). This module is the only code that touches it:

- `trigger_job(name)` — the in-sandbox trigger path: ONE bridge call to
  the audited `run_job` tool; the job body executes on the host
  (agent/jobs.py), where state/ and the Telegram token live.
- `install_cron()` — reads agent/schedule.yaml (still the ONE schedule
  config) and re-registers one cron job per §4.1 job via
  `nemoclaw <sandbox> exec -- openclaw cron …`. Re-syncing replaces the
  `herr-claw-*` jobs wholesale (run history resets) and removes drifted
  jobs no longer present in schedule.yaml.

Times in schedule.yaml are Berlin wall clock, so entries pin
`--tz Europe/Berlin` regardless of the sandbox's own timezone. The cron
command reaches the bridge over the OpenShell host alias — the only host
address the sandbox can reach — via HERR_BRIDGE_URL in the job env.
"""

from __future__ import annotations

import json
import os
import subprocess

from .bridge import BridgeClient, BridgeError
from .config import load_config
from .daemon import load_schedule

SANDBOX_ENV_VAR = "HERR_NEMOCLAW_SANDBOX"
DEFAULT_SANDBOX = "my-assistant"
SCHEDULE_TZ = "Europe/Berlin"
SANDBOX_BRIDGE_URL = "http://host.openshell.internal:8765/mcp"
SANDBOX_APP_DIR = "/sandbox/herrclaw"
SANDBOX_HERR_CLAW = f"{SANDBOX_APP_DIR}/.venv/bin/herr-claw"
CRON_JOB_PREFIX = "herr-claw-"
JOB_TIMEOUT_S = 120  # calendar/reminder round-trips can be slow; cron default is 30 s


def sandbox_name() -> str:
    return os.environ.get(SANDBOX_ENV_VAR, "").strip() or DEFAULT_SANDBOX


def trigger_job(name: str, *, cfg=None, client=None, output_fn=print) -> int:
    """One trigger → one audited `run_job` call on the host bridge."""
    cfg = cfg or load_config()
    if not cfg.bridge_url:
        output_fn("⚠︎ HERR_BRIDGE_URL ist 'off' — kein Trigger-Pfad zur Bridge.")
        return 1
    try:
        result = (client or BridgeClient(cfg.bridge_url)).call_tool("run_job", {"name": name})
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


def install_cron(
    *,
    jobs: dict | None = None,
    runner=None,
    sandbox: str = "",
    output_fn=print,
) -> int:
    """Register one OpenClaw cron job per schedule.yaml entry, idempotently:
    ALL existing `herr-claw-*` jobs are removed (by id — dedupes re-syncs),
    then every wanted job is added fresh. Non-herr-claw jobs are never
    touched."""
    jobs = jobs if jobs is not None else load_schedule()
    sandbox = sandbox or sandbox_name()
    runner = runner or _default_runner

    rc, out = runner(sandbox, ["openclaw", "cron", "list", "--json"])
    if rc != 0:
        output_fn(f"⚠︎ openclaw cron list fehlgeschlagen in Sandbox „{sandbox}“:\n{out.strip()}")
        return 1
    existing = _cron_jobs_from_list(out)

    wanted = {f"{CRON_JOB_PREFIX}{name}": (name, _cron_expr(t)) for name, t in jobs.items()}

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

    for job_name, (trigger, expr) in sorted(wanted.items(), key=lambda item: item[1][1]):
        rc, out = runner(
            sandbox,
            [
                "openclaw", "cron", "add",
                "--name", job_name,
                "--cron", expr,
                "--tz", SCHEDULE_TZ,
                "--exact",
                "--no-deliver",  # the host job sends Telegram itself; stdout stays in the cron log
                "--description", f"Herr Claw §4.1 {trigger} (aus agent/schedule.yaml)",
                "--timeout-seconds", str(JOB_TIMEOUT_S),
                "--command", f"{SANDBOX_HERR_CLAW} daemon --trigger {trigger}",
                "--command-cwd", SANDBOX_APP_DIR,
                "--command-env", f"HERR_BRIDGE_URL={SANDBOX_BRIDGE_URL}",
            ],
        )
        if rc != 0:
            output_fn(f"⚠︎ Cron-Job „{job_name}“ konnte nicht angelegt werden:\n{out.strip()}")
            failed = True
        else:
            output_fn(f"⏰ „{job_name}“ → {expr} ({SCHEDULE_TZ}) — ausgelöst wird auf dem Host.")

    if not failed:
        output_fn(
            f"✓ {len(wanted)} Cron-Job(s) in Sandbox „{sandbox}“ registriert — "
            "der OpenClaw-cron ist der EINE Scheduler."
        )
    return 1 if failed else 0
