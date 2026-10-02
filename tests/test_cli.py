"""CLI surface (Typer app): herr-claw chat / sprechen / bridge / turn /
trigger / telegram-loop / telegram-watchdog / install-cron / --help."""

from typer.testing import CliRunner

import main

runner = CliRunner()


def test_help_lists_subcommands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    assert "chat" in result.output
    assert "sprechen" in result.output
    assert "turn" in result.output  # P6: the brain surface
    assert "telegram-loop" in result.output  # P6: the in-sandbox receiver
    assert "install-cron" in result.output


def test_bare_invocation_prints_help_and_exits_zero():
    result = runner.invoke(main.app, [])
    assert result.exit_code == 0
    assert "chat" in result.output and "sprechen" in result.output


def test_sprechen_dispatches_to_run_sprechen(monkeypatch):
    called = {}

    def fake_run_sprechen(cfg=None, **kwargs):
        called["ran"] = True
        return 0

    monkeypatch.setattr("voice.loop.run_sprechen", fake_run_sprechen)
    result = runner.invoke(main.app, ["sprechen"])
    assert result.exit_code == 0
    assert called["ran"] is True


def test_sprechen_help_mentions_voice_pipeline():
    result = runner.invoke(main.app, ["sprechen", "--help"])
    assert result.exit_code == 0
    assert "Phase 3" in result.output


def test_unknown_command_is_rejected():
    result = runner.invoke(main.app, ["blabla"])
    assert result.exit_code != 0


def test_chat_command_help_mentions_terminal():
    result = runner.invoke(main.app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "Phase 1" in result.output


def test_chat_dispatches_to_run_chat(monkeypatch):
    called = {}

    def fake_run_chat():
        called["ran"] = True
        return 0

    monkeypatch.setattr("agent.chat.run_chat", fake_run_chat)
    result = runner.invoke(main.app, ["chat"])
    assert result.exit_code == 0
    assert called["ran"] is True


def test_trigger_dispatches_to_trigger_job(monkeypatch):
    called = {}

    def fake_trigger_job(job):
        called["job"] = job
        return 0

    monkeypatch.setattr("agent.cron_sync.trigger_job", fake_trigger_job)
    result = runner.invoke(main.app, ["trigger", "quiz"])
    assert result.exit_code == 0
    assert called["job"] == "quiz"


def test_install_cron_dispatches_to_cron_sync(monkeypatch):
    called = {}

    def fake_install_cron():
        called["ran"] = True
        return 0

    monkeypatch.setattr("agent.cron_sync.install_cron", fake_install_cron)
    result = runner.invoke(main.app, ["install-cron"])
    assert result.exit_code == 0
    assert called["ran"] is True


def test_trigger_help_mentions_the_openclaw_cron():
    result = runner.invoke(main.app, ["trigger", "--help"])
    assert result.exit_code == 0
    assert "OpenClaw-cron" in result.output  # the ONE scheduler (P5)
    assert "nudge" in result.output  # the fixed job names are visible


def test_turn_dispatches_to_run_turn_cli(monkeypatch):
    called = {}

    def fake_run_turn_cli(message, json_out=False, **_kwargs):
        called["message"] = message
        called["json_out"] = json_out
        return 0

    monkeypatch.setattr("agent.turns.run_turn_cli", fake_run_turn_cli)
    result = runner.invoke(main.app, ["turn", "Hallo!", "--json"])
    assert result.exit_code == 0
    assert called["message"] == "Hallo!" and called["json_out"] is True


def test_turn_help_mentions_the_brain():
    result = runner.invoke(main.app, ["turn", "--help"])
    assert result.exit_code == 0
    assert "Gehirn" in result.output  # P6: the brain surface


def test_telegram_loop_dispatches_to_poller(monkeypatch):
    called = {}

    def fake_run_poller(**_kwargs):
        called["ran"] = True
        return 0

    monkeypatch.setattr("agent.poller.run_poller", fake_run_poller)
    result = runner.invoke(main.app, ["telegram-loop"])
    assert result.exit_code == 0
    assert called["ran"] is True


def test_telegram_watchdog_dispatches_to_poller(monkeypatch):
    called = {}

    def fake_run_watchdog(**_kwargs):
        called["ran"] = True
        return 0

    monkeypatch.setattr("agent.poller.run_watchdog", fake_run_watchdog)
    result = runner.invoke(main.app, ["telegram-watchdog"])
    assert result.exit_code == 0
    assert called["ran"] is True


def test_daemon_subcommand_stays_gone():
    result = runner.invoke(main.app, ["daemon", "--help"])
    assert result.exit_code != 0  # removed in P5/P6 — must not come back


def test_cli_wrapper_returns_exit_code_int():
    assert main.cli(["--help"]) == 0
