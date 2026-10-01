"""Entry point for the `herr-claw` command (pyproject [project.scripts]).

Typer-based CLI; subcommands: `chat` (P1), `bridge` (P2, MCP on
127.0.0.1:8765), `sprechen` (P3), `daemon` (P4, daily loop + Telegram).
`cli()` wraps the Typer app so the console-script pin `herr-claw = "main:cli"`
keeps working.

Loads the host .env in fallback mode via python-dotenv — the file's contents
are never read or logged by hand; the OpenShell gateway provides the same
variables in sandbox mode.
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
    """Text-Chat im Terminal (Phase 1)."""
    from agent.chat import run_chat

    raise typer.Exit(run_chat())


@app.command()
def bridge() -> None:
    """MCP-Bridge starten: Apple Reminders/Calendar + Obsidian auf 127.0.0.1:8765 (Phase 2)."""
    from herrclaw_bridge.server import run_server

    raise typer.Exit(run_server())


@app.command()
def sprechen() -> None:
    """Sprachmodus: Mikrofon → Whisper (de) → Nemotron → Anna (Phase 3)."""
    from voice.loop import run_sprechen

    raise typer.Exit(run_sprechen())


@app.command()
def daemon() -> None:
    """Täglich-Schleife: Nudge/Quiz/Recap + Telegram (Phase 4)."""
    from agent.daemon import run_daemon

    raise typer.Exit(run_daemon())


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
