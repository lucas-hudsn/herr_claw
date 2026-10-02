"""The in-sandbox Telegram conversation receiver + its watchdog (P6).

`telegram-loop` is the ONE conversation receiver: it long-polls getUpdates
and grades every incoming message through the same brain as the TUI and
voice (agent.turns.LocalBrain) — slash commands, quizzes, corrections all
behave identically everywhere. It runs INSIDE sandbox `my-assistant`:
egress to api.telegram.org is allowlisted, and the token is the gateway's
`openshell:resolve:env:` placeholder (cron entry env), resolved at egress.

`telegram-watchdog` is the self-healing half: an idempotent cron fire
(every 5 min, agent/schedule.yaml `watchdogs:`) that starts the loop if
its pid is gone. The ONE scheduler keeps the ONE receiver alive.

Fence: the bot answers exactly ONE chat — HERR_TELEGRAM_CHAT_ID. Messages
from any other chat are ignored (they are not even answered with an
error), and without that variable the poller refuses to start.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import load_config
from .telegram import TelegramClient, TelegramError, client_from_env

POLL_TIMEOUT_S = 30
ERROR_BACKOFF_S = 5
PIDFILE_NAME = "telegram.pid"
LOG_NAME = "telegram.log"


def run_poller(
    cfg=None,
    *,
    client: TelegramClient | None = None,
    brain=None,
    get_updates_fn=None,
    output_fn=print,
    sleep_fn=time.sleep,
) -> int:
    """Long-poll loop: message in → brain.turn → reply out. Transport
    hiccups back off and retry (the loop is meant to run for days); a 409
    surfaces as TelegramError and self-heals when the other poller stops."""
    from .turns import LocalBrain, TurnError

    cfg = cfg or load_config()
    chat_id = cfg.telegram_chat_id
    if not chat_id:
        output_fn(
            "⚠︎ HERR_TELEGRAM_CHAT_ID ist nicht gesetzt — der Bot antwortet nur "
            "genau einem Chat; ohne die Variable startet die Schleife nicht."
        )
        return 1
    client = client or client_from_env()
    brain = brain or LocalBrain(cfg)
    get_updates = get_updates_fn or client.get_updates
    try:
        client.get_me()  # fail fast on a bad token / placeholder resolution
    except TelegramError as exc:
        output_fn(f"⚠︎ {exc}")
        return 1

    output_fn("📬 Telegram-Loop läuft — Strg-C beendet.")
    offset: int | None = None
    try:
        while True:
            try:
                updates = get_updates(offset=offset, timeout_s=POLL_TIMEOUT_S)
            except TelegramError as exc:
                output_fn(f"⚠︎ {exc} — neuer Versuch in {ERROR_BACKOFF_S} s.")
                sleep_fn(ERROR_BACKOFF_S)
                continue
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                message = update.get("message") or {}
                chat = str((message.get("chat") or {}).get("id", ""))
                text = str(message.get("text") or "").strip()
                if not text:
                    continue
                if chat != chat_id:
                    output_fn(f"⚠︎ Nachricht aus fremdem Chat ignoriert (nicht {chat_id}).")
                    continue
                try:
                    result = brain.turn(text)
                except TurnError as exc:
                    client.send_message(chat, f"⚠︎ {exc}")
                    continue
                client.send_message(chat, result.reply)
    except KeyboardInterrupt:
        pass
    finally:
        brain.close()
    output_fn("Telegram-Loop beendet.")
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, someone else's — count it as alive


def _loop_command() -> str:
    override = os.environ.get("HERR_TELEGRAM_LOOP_CMD", "").strip()
    if override:
        return override
    on_path = shutil.which("herr-claw")
    if on_path:
        return on_path
    return os.path.abspath(sys.argv[0])


def _default_spawner(loop_cmd: str, log_path: Path) -> int:
    """Start the loop detached (session leader — survives the watchdog
    fire's exit), output appended to the log; return the new pid."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [loop_cmd, "telegram-loop"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return proc.pid


def run_watchdog(
    cfg=None,
    *,
    pid_path: Path | None = None,
    spawner=None,
    alive_fn=_pid_alive,
    output_fn=print,
) -> int:
    """Idempotent: a healthy poller is a no-op, a dead one is restarted
    detached (output → telegram.log beside the pidfile). The watchdog owns
    the pidfile: the spawner only starts the process and returns its pid."""
    cfg = cfg or load_config()
    pid_path = pid_path or Path(cfg.state_dir) / PIDFILE_NAME
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = pid_path.parent / LOG_NAME
    spawner = spawner or _default_spawner

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = 0
        if pid > 0 and alive_fn(pid):
            return 0  # healthy — nothing to do (cron-safe: never double-starts)
        pid_path.unlink(missing_ok=True)  # stale pidfile

    pid = spawner(_loop_command(), log_path)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    output_fn(f"🚀 Telegram-Loop gestartet (pid {pid}, Log: {log_path}).")
    return 0
