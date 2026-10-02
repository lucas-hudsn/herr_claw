"""Entry point for the `herr-claw` command (pyproject [project.scripts]).

Typer-based CLI; subcommands: `chat` (P1, v0.5 TUI; P6 thin client over the
sandbox brain), `bridge` (P2, MCP on 127.0.0.1:8765 — the ONE Apple/vault
door), `sprechen` (P3, voice frontend), `turn` (P6, the brain surface the
sandbox runs per user message), `trigger <job>` (P5/P6, the OpenClaw-cron
trigger path — executes the job body in the sandbox), `telegram-loop` +
`telegram-watchdog` (P6, the in-sandbox conversation receiver and its
self-healing cron fire) and `install-cron` (P5, registers the cron jobs
from agent/schedule.yaml). There is no host daemon: the OpenClaw cron is
the ONE scheduler and the sandbox telegram-loop is the ONE receiver.
`cli()` wraps the Typer app so the console-script pin `herr-claw = "main:cli"`
keeps working.

Loads the host .env via python-dotenv — the file's contents are never read
or logged by hand; the OpenShell gateway provides the same variables in
sandbox mode (the cron entries carry the non-secret values plus the token
PLACEHOLDER via --command-env).
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Herr Claw — dein sandboxierter Deutsch-Tutor (NVIDIA Claw Agent Challenge)",
    add_completion=False,
)


def _load_env() -> None:
    from agent.config import REPO_ROOT
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # no-op when absent (sandbox mode)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """Load env, then dispatch. Bare invocation prints help (exit 0)."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    _load_env()


@app.command()
def chat() -> None:
    """Text-Chat im Terminal (Phase 1; P6: Dünnschicht über das Sandbox-Gehirn)."""
    from agent.chat import run_chat

    raise typer.Exit(run_chat())


@app.command()
def bridge() -> None:
    """MCP-Bridge starten: Apple Reminders/Calendar + Obsidian auf 127.0.0.1:8765 (Phase 2)."""
    from herrclaw_bridge.server import run_server

    raise typer.Exit(run_server())


@app.command()
def sprechen() -> None:
    """Sprachmodus: Mikrofon → Whisper (de) → Sandbox-Gehirn → Anna (Phase 3)."""
    from voice.loop import run_sprechen

    raise typer.Exit(run_sprechen())


@app.command()
def turn(
    message: str = typer.Argument(None, help="Die Nutzer-Nachricht; leer → stdin lesen."),
    json_out: bool = typer.Option(False, "--json", help="Antwort + Status als JSON (für die Frontends)."),
) -> None:
    """EINEN Tutor-Turn ausführen — das Gehirn (P6). Läuft in der Sandbox: ein
    Prozess pro Nachricht, Zustand in HERR_STATE_DIR; die Host-Frontends rufen
    das per `nemoclaw exec` auf."""
    from agent.turns import run_turn_cli

    raise typer.Exit(run_turn_cli(message, json_out=json_out))


@app.command()
def trigger(
    job: str = typer.Argument(
        ...,
        help="Einmalig auszuführender Job: 'nudge' | 'quiz' | 'recap' (aus agent/schedule.yaml).",
    ),
) -> None:
    """Einen Tages-Job ausführen (Triggerpfad des OpenClaw-cron; läuft in der
    Sandbox — der Job-Body läuft HIER, Apple/Vault-Aufrufe über die Bridge,
    Dedup über session.json)."""
    from agent.cron_sync import trigger_job

    raise typer.Exit(trigger_job(job))


@app.command("install-cron")
def install_cron() -> None:
    """Cron-Jobs aus agent/schedule.yaml idempotent in der Sandbox registrieren
    (der OpenClaw-cron ist der EINE Scheduler; inkl. Telegram-Watchdog)."""
    from agent.cron_sync import install_cron as _install_cron

    raise typer.Exit(_install_cron())


@app.command("telegram-loop")
def telegram_loop() -> None:
    """Telegram-Empfänger (P6): Long-Poll auf getUpdates IN der Sandbox — jede
    Nachricht durch dasselbe Gehirn wie TUI/Voice; antwortet nur HERR_TELEGRAM_CHAT_ID."""
    from agent.poller import run_poller

    raise typer.Exit(run_poller())


@app.command("telegram-watchdog")
def telegram_watchdog() -> None:
    """Selbstheilung (P6): startet telegram-loop, wenn sein pid tot ist —
    idempotent, als Cron-Feuer alle 5 Minuten (agent/schedule.yaml)."""
    from agent.poller import run_watchdog

    raise typer.Exit(run_watchdog())


def cli(argv: list[str] | None = None) -> int:
    """Console-script entry: run the Typer app and return the exit code."""
    try:
        app(args=argv, prog_name="herr-claw")
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
