"""End-to-end chat loop over the real brain seam (P6): run_chat builds the
default brain from config; a stubbed LocalBrain (FakeTutor) proves the loop
renders replies, routes raw text, and shuts the brain down cleanly. The
brain's own behavior (persistence, commands, quizzes) is test_turns.py."""

from agent.chat import run_chat
from agent.turns import TurnResult


class FakeTutor:
    def __init__(self):
        self.calls = []

    def reply(self, window, system_note=None):
        self.calls.append({"window": list(window), "system_note": system_note})
        return f"Antwort Nummer {len(self.calls)}. Wie geht es dir heute?"


def make_io(responses):
    iterator = iter(responses)
    outputs: list[str] = []

    def input_fn(prompt=""):
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return input_fn, outputs.append, outputs


def test_chat_loop_over_the_real_brain_seam(monkeypatch, config):
    built = []

    class StubLocal:
        def __init__(self, cfg):
            self.cfg = cfg
            self.tutor = FakeTutor()
            self.closed = False
            built.append(self)

        def turn(self, text):
            return TurnResult(reply=self.tutor.reply([]), streak=1, due=2, topic="")

        def close(self):
            self.closed = True

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)

    input_fn, output_fn, outputs = make_io(["Hallo!", "/fortschritt", "Tschüss-Satz"])
    code = run_chat(cfg=config, input_fn=input_fn, output_fn=output_fn, tty=False)

    assert code == 0
    assert len(built) == 1
    assert built[0].cfg is config  # config flows into the brain untouched
    assert len(built[0].tutor.calls) == 3  # every line (commands included) is a brain turn now
    assert sum("Antwort Nummer" in line for line in outputs) == 3
    assert any("Tschüss" in line for line in outputs)  # goodbye after real turns
    assert built[0].closed


def test_chat_loop_survives_an_empty_line(monkeypatch, config):
    built = []

    class StubLocal:
        def __init__(self, cfg):
            self.texts = []
            self.closed = False
            built.append(self)

        def turn(self, text):
            self.texts.append(text)
            return TurnResult(reply="ok")

        def close(self):
            self.closed = True

    monkeypatch.setattr("agent.turns.LocalBrain", StubLocal)
    input_fn, output_fn, outputs = make_io(["", "   ", "Hallo!"])
    code = run_chat(cfg=config, input_fn=input_fn, output_fn=output_fn, tty=False)
    assert code == 0
    assert built[0].texts == ["Hallo!"]  # empty lines never reach the brain
    assert any("Herr Claw: ok" in line for line in outputs)
