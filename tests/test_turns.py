"""The P6 brain seam: LocalBrain (one-shot turns, cross-process continuity),
SandboxBrain (thin client), AutoBrain (sandbox→local fallback), turn CLI.

LocalBrain tests run with a FakeTutor, tmp state and a fake bridge — no
network, no Apple, no nemoclaw. They prove the property the whole P6 design
rests on: state lives in HERR_STATE_DIR, so separately-started brains
continue each other's history, quizzes and sessions."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from agent.turns import (
    AutoBrain,
    LocalBrain,
    SandboxBrain,
    TurnError,
    TurnResult,
    run_turn_cli,
)


class FakeTutor:
    def __init__(self):
        self.calls = []

    def reply(self, window, system_note=None):
        self.calls.append({"window": list(window), "system_note": system_note})
        n = len(self.calls)
        if n == 2:
            return "Fast! Richtig: Ich ging zum Bäcker / Deine Version: Ich bin ging zum Bäcker."
        return f"Antwort Nummer {n}. Wie geht es dir?"


class ScriptedTutor:
    """Replies in script order — for tests that need exact per-turn text."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def reply(self, window, system_note=None):
        self.calls.append({"window": list(window), "system_note": system_note})
        return self.replies.pop(0)


class FakeBridge:
    """call_tool-compatible fake: freebusy events, else records the call."""

    def __init__(self, events=None, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.events = events or []
        self.fail = fail

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self.fail:
            from herrclaw_bridge.errors import BridgeError

            raise BridgeError("Bridge nicht erreichbar")
        if name == "calendar_freebusy":
            return json.dumps({"events": self.events})
        if name == "vault_read":
            raise FileNotFoundError(arguments["rel_path"])
        if name == "vault_append":
            return f"Angehängt: {arguments['rel_path']}"
        raise AssertionError(f"unerwartetes Tool {name}")


EVENT = {  # today 15:00, keyword "Bäcker" → "Beim Bäcker"
    "title": "Beim Bäcker treffen",
    "start": datetime.now().replace(hour=15, minute=0).isoformat(timespec="minutes"),
}


def make_brain(config, *, tutor=None, bridge=None, now_fn=datetime.now):
    return LocalBrain(
        config,
        tutor=tutor or FakeTutor(),
        bridge=bridge,
        now_fn=now_fn,
    )


# -- one-shot turns persist everything ---------------------------------------------


def test_turn_persists_history_totals_and_streak(config):
    brain = make_brain(config)
    result = brain.turn("Hallo!")
    assert result.reply == "Antwort Nummer 1. Wie geht es dir?"
    assert result.speakable and result.streak == 1

    from agent.state import SrsState

    srs = SrsState(config.srs_path)
    assert srs.totals.messages == 1 and srs.totals.corrections == 0

    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    assert [m["content"] for m in session.history] == ["Hallo!", "Antwort Nummer 1. Wie geht es dir?"]
    assert session.session_turns == 1 and session.last_activity != ""


def test_history_continues_across_separate_brain_processes(config):
    first = make_brain(config, tutor=ScriptedTutor(["Antwort Nummer 1. Wie geht es dir?"]))
    first.turn("Hallo!")

    # a fresh process (fresh brain, fresh tutor) picks the conversation up
    second = make_brain(
        config,
        tutor=ScriptedTutor(["Fast! Richtig: Ich ging zum Bäcker / Deine Version: Ich bin ging zum Bäcker."]),
    )
    second.turn("Ich bin ging zum Bäcker.")

    from agent.state import SrsState

    srs = SrsState(config.srs_path)
    assert srs.totals.corrections == 1
    assert srs.mistakes[0].fix == "Ich ging zum Bäcker"

    # the LLM window of a fresh process carries the earlier conversation,
    # including the correction reply this process just produced
    assert [m["content"] for m in second.history] == [
        "Hallo!",
        "Antwort Nummer 1. Wie geht es dir?",
        "Ich bin ging zum Bäcker.",
        "Fast! Richtig: Ich ging zum Bäcker / Deine Version: Ich bin ging zum Bäcker.",
    ]


def test_history_window_is_capped(config):
    from agent.turns import MAX_HISTORY_MESSAGES

    brain = make_brain(config)
    for i in range(MAX_HISTORY_MESSAGES + 4):
        brain.turn(f"Nachricht {i}")

    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    assert len(session.history) == MAX_HISTORY_MESSAGES
    assert session.history[-1]["content"].startswith("Antwort")


def test_vault_writes_fehler_progress_daily_note(config):
    tutor = ScriptedTutor([
        "Antwort Nummer 1. Wie geht es dir?",
        "Fast! Richtig: Ich ging einkaufen / Deine Version: Ich bin ging einkaufen.",
    ])
    brain = make_brain(config, tutor=tutor)
    brain.turn("Hallo!")
    brain.turn("Ich bin ging einkaufen.")
    brain.close()  # the frontend closes the session on exit (like the old loop's finally)

    from datetime import datetime

    from herrclaw_bridge.vault import Vault

    vault = Vault(config.vault_root, audit_log=config.state_dir / "audit.log")
    stamp = datetime.now()
    fehler = vault.read("Deutsch/fehler.md")
    assert "Deine Version" in fehler and "Richtig" in fehler
    progress = vault.read("Deutsch/progress.md")
    assert "Streak: 1 Tag(e)" in progress and "Gesamt: 2 Nachrichten" in progress
    daily = vault.read(stamp.strftime("Daily notes/%Y-%m-%d-deutsch.md"))
    assert "Chat mit Herr Claw" in daily and "2 Nachrichten" in daily


def test_llm_error_raises_turn_error_and_keeps_window_clean(config):
    class ExplodingTutor:
        def reply(self, window, system_note=None):
            from agent.llm import LLMError

            raise LLMError("Netzwerk weg")

    brain = make_brain(config, tutor=ExplodingTutor())
    with pytest.raises(TurnError, match="Netzwerk weg"):
        brain.turn("Hallo!")

    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    assert session.history == []  # the failed turn left no trace


def test_direct_commands_are_not_speakable_and_skip_the_tutor(config):
    tutor = FakeTutor()
    brain = make_brain(config, tutor=tutor)
    result = brain.turn("/fortschritt")
    assert result.speakable is False
    assert "Streak" in result.reply
    assert tutor.calls == []  # command output never hit the LLM
    # …but the next plain message still goes through the tutor
    assert make_brain(config, tutor=tutor).turn("Hallo!").speakable


# -- quiz continuity across processes ---------------------------------------------


def test_quiz_started_by_one_process_is_graded_by_the_next(config):
    first = make_brain(config)
    intro = first.turn("/quiz 3")
    assert "Frage 1/3" in intro.reply

    from agent.state import SessionState

    persisted = SessionState.load(config.session_path).active_quiz
    assert persisted["questions"] and persisted["index"] == 0

    second = make_brain(config)  # a fresh process (the Telegram poller, say)
    answer = second.turn("ende")  # quit word ends the restored quiz
    assert "Quiz beendet" in answer.reply

    # a finished quiz is cleared, not resurrected
    assert SessionState.load(config.session_path).active_quiz == {}


def test_job_quiz_persists_for_the_poller(config):
    """The cron quiz fire (JobRunner) must leave a quiz the next `turn`
    process can grade — the P4 dead-end (quiz died with the process) is gone."""
    from agent.jobs import JobRunner
    from agent.quiz import start as start_quiz  # noqa: F401 — parity with jobs.py import
    from agent.state import SessionState, SrsState

    srs = SrsState(config.srs_path)
    session = SessionState.load(config.session_path)
    runner = JobRunner(
        config, srs=srs, session=session, bridge=None, vault=None,
        now_fn=lambda: datetime.now(), output_fn=lambda line: None,
    )
    runner.session.jobs_done["nudge"] = datetime.now().date().isoformat()
    assert runner.run("quiz", datetime.now()) is True  # the real cron path (marks done + saves)

    assert session.active_quiz["questions"]
    poller_brain = make_brain(config)
    result = poller_brain.turn("Brot")  # graded against the restored quiz
    assert ("Richtig" in result.reply) or ("falsch" in result.reply)


# -- session gap close --------------------------------------------------------------


def test_idle_gap_closes_the_previous_session(config):
    brain = make_brain(config)
    brain.turn("Hallo!")
    brain.turn("Und noch eins!")

    from agent.state import SessionState, SrsState

    stale = datetime.now() - timedelta(hours=2)
    session = SessionState.load(config.session_path)
    session.last_activity = stale.isoformat(timespec="seconds")
    session.save()

    later = make_brain(config, now_fn=lambda: datetime.now())  # fresh process, 2h later
    result = later.turn("Nach dem Schlaf!")

    srs = SrsState(config.srs_path)
    assert srs.totals.sessions == 1  # the stale session was closed, not merged
    fresh = SessionState.load(config.session_path)
    assert fresh.last_session_turns == 2
    assert fresh.session_turns == 1  # only the new turn counts in the open session
    assert result.reply.startswith("Antwort")


def test_active_session_is_not_closed_within_the_gap(config):
    make_brain(config).turn("Hallo!")
    second = make_brain(config)
    second.turn("Und weiter!")
    assert second.session.session_turns == 2  # same open session


# -- calendar topic flourish (brain-side now) ---------------------------------------


def test_topic_from_calendar_sets_the_startup_note(monkeypatch, config):
    bridge = FakeBridge(events=[EVENT])
    brain = make_brain(config, bridge=bridge)
    result = brain.turn("Hallo!")
    assert brain.session.current_topic == "Beim Bäcker"
    # the flourish rides the first LLM turn's system note
    assert any("Beim Bäcker" in (c["system_note"] or "") for c in brain.tutor.calls)
    assert result.topic == "Beim Bäcker"


def test_existing_topic_wins_over_calendar(config):
    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    session.current_topic = "Wohnung"
    session.save()
    brain = make_brain(config, bridge=FakeBridge(events=[EVENT]))
    brain.turn("Hallo!")
    assert brain.session.current_topic == "Wohnung"
    assert all(c["system_note"] is None for c in brain.tutor.calls)


def test_dead_bridge_degrades_silently(config):
    brain = make_brain(config, bridge=FakeBridge(fail=True))
    result = brain.turn("Hallo!")
    assert result.reply.startswith("Antwort")  # no crash, no topic flourish
    assert brain.session.current_topic == ""


# -- SandboxBrain: the thin client ---------------------------------------------------


def test_sandbox_brain_parses_the_turn_json():
    payload = TurnResult(reply="Moin!", streak=2, due=5, topic="Sport").to_json()

    def runner(sandbox, text, timeout_s):
        assert sandbox == "my-assistant" and text == "Hallo"
        return 0, json.dumps(payload), ""

    result = SandboxBrain(runner=runner).turn("Hallo")
    assert result.reply == "Moin!" and result.streak == 2 and result.due == 5


def test_sandbox_brain_surfaces_stderr_as_turn_error():
    def runner(sandbox, text, timeout_s):
        return 1, "", "⚠︎ NVIDIA_API_KEY ist nicht gesetzt\n"

    with pytest.raises(TurnError, match="NVIDIA_API_KEY"):
        SandboxBrain(runner=runner).turn("Hallo")


def test_sandbox_brain_rejects_non_json_stdout():
    def runner(sandbox, text, timeout_s):
        return 0, "kein json", ""

    with pytest.raises(TurnError, match="Unerwartete Sandbox-Antwort"):
        SandboxBrain(runner=runner).turn("Hallo")


# -- AutoBrain: sandbox first, one warned local fallback ------------------------------


def test_autobrain_falls_back_to_local_with_one_warning(monkeypatch, config):
    warnings: list[str] = []

    class ExplodingSandbox:
        def turn(self, text):
            raise TurnError("bridge down")

        def close(self):
            pass

    class StubLocal:
        instances = []

        def __init__(self, cfg):
            self.texts = []
            StubLocal.instances.append(self)

        def turn(self, text):
            self.texts.append(text)
            return TurnResult(reply="lokal gerettet")

        def close(self):
            pass

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)

    brain = AutoBrain(config, output_fn=warnings.append)
    brain._sandbox = ExplodingSandbox()  # pretend the sandbox just died
    result = brain.turn("Hallo!")
    assert len(warnings) == 1 and "Break-Glass" in warnings[0] and "bridge down" in warnings[0]
    assert result.reply == "lokal gerettet"
    assert brain.turn("Noch was!").reply == "lokal gerettet"  # stays local — warned once
    assert len(StubLocal.instances) == 1


def test_autobrain_local_backend_skips_the_sandbox(monkeypatch, config):
    monkeypatch.setattr("agent.turns.SandboxBrain", pytest.fail)  # must never be built
    cfg = replace(config, chat_backend="local")
    brain = AutoBrain(cfg, output_fn=lambda s: None)
    assert brain._sandbox is None


# -- the `turn` CLI -------------------------------------------------------------------


def test_turn_cli_prints_plain_reply(monkeypatch, config):
    class StubLocal:
        def __init__(self, cfg):
            pass

        def turn(self, text):
            assert text == "Hallo!"
            return TurnResult(reply="Guten Tag!")

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)
    outputs = []
    code = run_turn_cli("Hallo!", cfg=config, output_fn=outputs.append)
    assert code == 0 and outputs == ["Guten Tag!"]


def test_turn_cli_json_output(monkeypatch, config):
    class StubLocal:
        def __init__(self, cfg):
            pass

        def turn(self, text):
            return TurnResult(reply="Guten Tag!", streak=3, due=7, topic="Sport")

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)
    outputs = []
    code = run_turn_cli("Hallo!", json_out=True, cfg=config, output_fn=outputs.append)
    assert code == 0
    data = json.loads(outputs[0])
    assert data["reply"] == "Guten Tag!" and data["streak"] == 3 and data["due"] == 7


def test_turn_cli_empty_message_fails_with_exit_1():
    outputs = []
    code = run_turn_cli("", read_input=lambda: "  ", output_fn=outputs.append)
    assert code == 1 and outputs == []


def test_turn_cli_reports_errors_on_stderr(monkeypatch, config, capsys):
    class StubLocal:
        def __init__(self, cfg):
            pass

        def turn(self, text):
            raise TurnError("Sandbox kaputt")

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)
    code = run_turn_cli("Hallo!", cfg=config, output_fn=lambda s: None)
    assert code == 1
    assert "Sandbox kaputt" in capsys.readouterr().err
