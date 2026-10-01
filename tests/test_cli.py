"""CLI surface (Typer app): herr-claw chat / sprechen (stub) / --help."""

from typer.testing import CliRunner

import main

runner = CliRunner()


def test_help_lists_subcommands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    assert "chat" in result.output
    assert "sprechen" in result.output


def test_bare_invocation_prints_help_and_exits_zero():
    result = runner.invoke(main.app, [])
    assert result.exit_code == 0
    assert "chat" in result.output and "sprechen" in result.output


def test_sprechen_is_an_honest_stub():
    result = runner.invoke(main.app, ["sprechen"])
    assert result.exit_code == 2
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


def test_cli_wrapper_returns_exit_code_int():
    assert main.cli(["--help"]) == 0
    assert main.cli(["sprechen"]) == 2
