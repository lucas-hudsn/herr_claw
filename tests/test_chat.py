"""End-to-end chat loop with a fake tutor — no network, real state + vault."""

from datetime import date

from agent.chat import run_chat


class FakeTutor:
    def __init__(self):
        self.calls = []

    def reply(self, window, system_note=None):
        self.calls.append({"window": list(window), "system_note": system_note})
        n = len(self.calls)
        if n == 2:
            return "Fast! Richtig: Ich ging zum Bäcker / Deine Version: Ich bin ging zum Bäcker."
        return f"Antwort Nummer {n}. Wie geht es dir heute?"


def make_io(responses):
    iterator = iter(responses)
    outputs: list[str] = []

    def input_fn(prompt=""):
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return input_fn, outputs.append, outputs


def test_three_turn_chat_persists_everything(config):
    from dataclasses import replace

    cfg = replace(config, vault_root=None)  # HERR_VAULT unset → warns, chat still works
    fake = FakeTutor()
    input_fn, output_fn, outputs = make_io(
        ["Hallo!", "Ich bin ging zum Bäcker.", "/fortschritt", "/quiz"]
    )
    code = run_chat(cfg=cfg, tutor=fake, input_fn=input_fn, output_fn=output_fn)

    assert code == 0
    # 2 LLM turns happened; slash commands did not hit the tutor
    assert len(fake.calls) == 2
    assert fake.calls[0]["window"][0]["content"] == "Hallo!"
    # second call's window carries the whole conversation, newest last
    assert [m["content"] for m in fake.calls[1]["window"]] == [
        "Hallo!",
        "Antwort Nummer 1. Wie geht es dir heute?",
        "Ich bin ging zum Bäcker.",
    ]
    assert any("Antwort Nummer 1" in line for line in outputs)
    assert any("Streak: 1" in line for line in outputs)  # /fortschritt recalled in-session
    assert any("2 Nachrichten, 1 Korrekturen" in line for line in outputs)  # at /fortschritt time
    assert any("Frage 1/5" in line for line in outputs)  # P4: /quiz really quizzes now
    assert any("HERR_VAULT" in line for line in outputs)  # one-time warning

    # state/srs.json persisted
    from agent.state import SrsState

    reloaded = SrsState(config.srs_path)
    assert reloaded.totals.messages == 2
    assert reloaded.totals.sessions == 1
    assert reloaded.streak.current == 1
    assert reloaded.streak.last_day == date.today()

    # session.json persisted
    from agent.state import SessionState

    session = SessionState.load(config.session_path)
    assert session.last_session_turns == 2
    assert session.last_session_end != ""


def test_correction_logged_and_restart_recall(config):
    fake = FakeTutor()
    input_fn, output_fn, outputs = make_io(
        ["Ich bin ging zum Bäcker.", "Und jetzt?", "/fehler", "/fortschritt"]
    )
    run_chat(cfg=config, tutor=fake, input_fn=input_fn, output_fn=output_fn)

    from agent.state import SrsState

    srs = SrsState(config.srs_path)
    assert len(srs.mistakes) == 1
    assert srs.mistakes[0].example == "Ich bin ging zum Bäcker."
    assert srs.mistakes[0].fix == "Ich ging zum Bäcker"

    # /fehler in-session shows it; a fresh run (restart) recalls it
    assert any("→ „Ich ging zum Bäcker“" in line for line in outputs)
    input_fn2, output_fn2, outputs2 = make_io(["/fehler", "/fortschritt"])
    run_chat(cfg=config, tutor=FakeTutor(), input_fn=input_fn2, output_fn=output_fn2)
    assert any("Deine letzten 1 Fehler" in line for line in outputs2)
    assert any("1 Sitzungen" in line for line in outputs2)


def test_vault_writes_fehler_progress_daily_note(config):
    fake = FakeTutor()
    input_fn, output_fn, _ = make_io(["Hallo!", "Ich bin ging einkaufen."])
    run_chat(cfg=config, tutor=fake, input_fn=input_fn, output_fn=output_fn)

    from datetime import datetime

    from herrclaw_bridge.vault import Vault

    vault = Vault(config.vault_root, audit_log=config.state_dir / "audit.log")
    stamp = datetime.now()
    fehler = vault.read("Deutsch/fehler.md")
    assert "Deine Version" in fehler and "Richtig" in fehler
    progress = vault.read("Deutsch/progress.md")
    assert f"Streak: 1 Tag(e)" in progress and "Gesamt: 2 Nachrichten" in progress
    daily_rel = stamp.strftime("Daily notes/%Y-%m-%d-deutsch.md")
    daily = vault.read(daily_rel)
    assert "Chat mit Herr Claw" in daily and "2 Nachrichten" in daily


def test_llm_error_saves_state_and_exits_cleanly(config):
    class ExplodingTutor:
        def reply(self, window, system_note=None):
            from agent.llm import LLMError

            raise LLMError("Keine Verbindung zu integrate.api.nvidia.com — Netzwerk prüfen.")

    input_fn, output_fn, outputs = make_io(["Hallo!"])
    code = run_chat(cfg=config, tutor=ExplodingTutor(), input_fn=input_fn, output_fn=output_fn)
    assert code == 0
    assert any("Keine Verbindung" in line for line in outputs)

    from agent.state import SessionState, SrsState

    assert SrsState(config.srs_path).totals.messages == 0  # failed turn not counted
    assert SessionState.load(config.session_path).last_session_turns == 0


# ---- P2: bridge wiring — topic-from-calendar flourish -----------------------


class FakeChatBridge:
    def __init__(self, events=None, fail=False):
        self.events = events or []
        self.fail = fail
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if self.fail:
            from agent.bridge import BridgeError

            raise BridgeError("Bridge nicht erreichbar")
        import json

        if name == "calendar_freebusy":
            return json.dumps(
                {
                    "events": [
                        {"title": t, "start": s, "end": e, "all_day": False}
                        for t, s, e in self.events
                    ]
                }
            )
        return "ok"


def _with_bridge(monkeypatch, config, bridge):
    from dataclasses import replace

    monkeypatch.setattr("agent.chat.BridgeClient", lambda url: bridge)
    return replace(config, bridge_url="http://127.0.0.1:59999/mcp")


def test_topic_derived_from_calendar_and_noted_once(monkeypatch, config):
    cfg = _with_bridge(
        monkeypatch, config, FakeChatBridge(events=[("Anmeldung Bürgeramt", "2026-10-02T16:00", "2026-10-02T16:30")])
    )
    fake = FakeTutor()
    input_fn, output_fn, outputs = make_io(["Hallo!", "Noch was!"])
    run_chat(cfg=cfg, tutor=fake, input_fn=input_fn, output_fn=output_fn)

    assert any("Kalender" in line and "Behörden und Termine" in line for line in outputs)
    # flourish rides on the FIRST LLM turn only
    assert fake.calls[0]["system_note"] and "Behörden und Termine" in fake.calls[0]["system_note"]
    assert fake.calls[1]["system_note"] is None

    from agent.state import SessionState

    assert SessionState.load(config.session_path).current_topic == "Behörden und Termine"


def test_existing_topic_wins_over_calendar(monkeypatch, config):
    cfg = _with_bridge(
        monkeypatch, config, FakeChatBridge(events=[("Anmeldung Bürgeramt", "2026-10-02T16:00", "2026-10-02T16:30")])
    )
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    config.session_path.write_text(
        '{"version": 1, "current_topic": "Bäckerei"}', encoding="utf-8"
    )
    fake = FakeTutor()
    input_fn, output_fn, outputs = make_io(["Hallo!"])
    run_chat(cfg=cfg, tutor=fake, input_fn=input_fn, output_fn=output_fn)
    assert not any("Kalender" in line for line in outputs)
    assert fake.calls[0]["system_note"] is None


def test_dead_bridge_degrades_silently(config):
    from dataclasses import replace

    cfg = replace(config, bridge_url="http://127.0.0.1:1/mcp")
    fake = FakeTutor()
    input_fn, output_fn, outputs = make_io(["Hallo!"])
    code = run_chat(cfg=cfg, tutor=fake, input_fn=input_fn, output_fn=output_fn)
    assert code == 0
    assert not any("Kalender" in line for line in outputs)  # flourish skipped, no crash
    assert fake.calls[0]["system_note"] is None
