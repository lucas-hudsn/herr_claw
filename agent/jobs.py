"""Host-side one-shot job execution — the `run_job` MCP tool's engine (P5).

The ONE scheduler is the OpenClaw cron inside sandbox `my-assistant`
(SPEC §4.1). A cron fire runs `herr-claw daemon --trigger <job>` inside the
sandbox, which calls the bridge tool `run_job`; this module executes the
job body on the HOST, where state/ and the Telegram token live:

- state stays in ./state/ — it never enters the sandbox (ONE state path);
- Apple/vault access goes through LocalBridge — the same allowlisted,
  audited component functions the MCP tools use, minus the HTTP hop;
- dedup stays in session.json (`jobs_done`), so cron fires, host debug
  runs, and the break-glass daemon loop can never double-fire a job.

The interactive Telegram long-poll (quiz answers, slash commands) remains
the break-glass `herr-claw daemon` loop's job — it shares this state and
this dedup, so both paths can run side by side.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config, load_config
from .daemon import Daemon, load_schedule
from .quiz import ensure_seeded
from .state import SessionState, SrsState
from .telegram import TelegramClient, TelegramError, client_from_env
from herrclaw_bridge.audit import audit
from herrclaw_bridge.errors import BridgeError
from herrclaw_bridge.local import LocalBridge
from herrclaw_bridge.server import vault_from_env as bridge_vault_from_env
from herrclaw_bridge.vault import Vault


def run_job_once(
    name: str,
    *,
    cfg: Config | None = None,
    vault: Vault | None = None,
    telegram: TelegramClient | None = None,
    bridge: LocalBridge | None = None,
    audit_log: Path | None = None,
    now: datetime | None = None,
    output_fn=None,
) -> str:
    """Run one §4.1 job host-side, exactly once per day (session.json dedup).

    All collaborators are injectable so tests never touch Telegram, Apple
    or the real audit log. Returns a German result text for the cron log;
    raises BridgeError for unknown job names (the MCP layer forwards it as
    a denial)."""
    cfg = cfg or load_config()
    if vault is None:
        vault = bridge_vault_from_env()
    if bridge is None:
        bridge = LocalBridge(vault)
    if telegram is None:
        try:
            telegram = client_from_env()
        except TelegramError:
            telegram = None  # no token → jobs still run, sends are skipped

    jobs = load_schedule()
    if name not in jobs:
        audit(
            "job.run",
            name,
            allowed=False,
            reason="unbekannter Job (nicht in agent/schedule.yaml)",
            log_path=audit_log,
        )
        raise BridgeError(
            f"Unbekannter Job „{name}“ — erlaubt sind: {', '.join(sorted(jobs))}."
        )

    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        if output_fn is not None:
            output_fn(line)

    srs = SrsState(cfg.srs_path)
    ensure_seeded(srs)
    session = SessionState.load(cfg.session_path)
    now = now or datetime.now()
    if session.jobs_done.get(name) == now.date().isoformat():
        # The daemon loop dedups in pending_jobs(); the trigger path checks
        # here instead — either way a job runs at most once per day.
        return f"Job „{name}“ lief heute schon — übersprungen (Dedup über session.json)."
    daemon = Daemon(
        cfg,
        srs=srs,
        session=session,
        telegram=telegram,
        chat_id=cfg.telegram_chat_id,
        bridge=bridge,
        vault=vault,
        jobs=jobs,
        output_fn=collect,
    )
    ok = daemon._run_job(name, now)
    audit("job.run", name, allowed=True, log_path=audit_log)
    status = "erledigt" if ok else "fehlgeschlagen (wird beim nächsten Feuer erneut versucht)"
    header = f"Job „{name}“ {status}."
    return "\n".join([header, *lines]) if lines else header
