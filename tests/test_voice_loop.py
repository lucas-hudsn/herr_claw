"""voice.loop — the sprechen loop with fakes for mic/STT/TTS, plus the
KeyPrompt line editor. P6: the loop is a thin client — the brain seam is
faked (FakeBrain), so nothing here touches state, the LLM, or the network."""

import numpy as np
import pytest

from agent.turns import TurnError, TurnResult
from voice.loop import KeyPrompt, run_sprechen


class FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def say(self, text):
        self.spoken.append(text)


class FakeBrain:
    """Script items: TurnResult (returned) or Exception (raised)."""

    def __init__(self, results):
        self.items = list(results)
        self.texts = []
        self.closed = False

    def turn(self, text):
        self.texts.append(text)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class FakePrompt:
    """Script items: "space" fires on_space(), "quit" → None, any str is a
    submitted line."""

    def __init__(self, script):
        self.script = list(script)

    def read(self, on_space):
        self.on_space = on_space
        while self.script:
            item = self.script.pop(0)
            if item == "space":
                on_space()
                continue
            if item == "quit":
                return None
            return item
        return None


_UNSET = object()


def run(cfg, script, *, brain=None, transcribe=None, record_audio=_UNSET, speaker=None):
    output = []
    rc = run_sprechen(
        cfg,
        brain=brain or FakeBrain([TurnResult(reply="Alles klar!")]),
        transcribe=transcribe or (lambda audio: "Guten Tag"),
        speaker=speaker or FakeSpeaker(),
        record_audio=(
            record_audio
            if record_audio is not _UNSET
            else (lambda: np.zeros(1600, dtype=np.float32))
        ),
        key_prompt=FakePrompt(script),
        output_fn=output.append,
    )
    return rc, output


# -- voice turns ---------------------------------------------------------------

def test_voice_turn_transcribes_replies_speaks(config):
    spoken = FakeSpeaker()
    reply = "Müde ist okay! Schlaf gut!"
    brain = FakeBrain([TurnResult(reply=reply, speakable=True)])
    rc, output = run(
        config,
        ["space", "q"],
        brain=brain,
        transcribe=lambda audio: "ich bin müd",
        speaker=spoken,
    )

    assert rc == 0
    assert any("Du 🗣️: ich bin müd" in line for line in output)
    assert any("Herr Claw: Müde ist okay!" in line for line in output)
    assert any("⏱" in line for line in output)  # round-trip budget on screen
    assert spoken.spoken == [reply]
    assert brain.texts == ["ich bin müd"]  # the transcript is the brain's input
    assert brain.closed  # the loop shuts the brain down (session end lives there)


def test_correction_is_spoken_too(config):
    spoken = FakeSpeaker()
    reply = "Richtig: Ich bin müde. / Deine Version: ich bin müd."
    brain = FakeBrain([TurnResult(reply=reply, speakable=True)])
    rc, _ = run(
        config,
        ["space", "q"],
        brain=brain,
        transcribe=lambda audio: "ich bin müd",
        speaker=spoken,
    )
    assert spoken.spoken == [reply]


def test_silence_never_reaches_stt_or_brain(config):
    transcribe = lambda audio: pytest.fail("STT called without audio")  # noqa: E731
    brain = FakeBrain([TurnResult(reply="Sollte nie kommen")])
    rc, output = run(
        config,
        ["space", "q"],
        brain=brain,
        transcribe=transcribe,
        record_audio=lambda: np.zeros(0, dtype=np.float32),
    )
    assert rc == 0
    assert any("nichts gehört" in line for line in output)
    assert brain.texts == []


def test_unrecognized_speech_asks_again_and_speaks(config):
    spoken = FakeSpeaker()
    brain = FakeBrain([TurnResult(reply="Sollte nie kommen")])
    rc, output = run(
        config,
        ["space", "q"],
        brain=brain,
        transcribe=lambda audio: "",
        speaker=spoken,
    )
    assert any("nicht verstanden" in line for line in output)
    assert spoken.spoken == ["Das habe ich nicht verstanden. Nochmal, bitte!"]
    assert brain.texts == []


def test_brain_error_keeps_the_loop_alive(config):
    brain = FakeBrain([TurnError("Rate-Limit"), TurnResult(reply="Zweiter Versuch klappt!")])
    rc, output = run(
        config,
        ["space", "space", "q"],
        brain=brain,
        transcribe=lambda audio: "Hallo",
    )
    assert rc == 0
    assert sum("⚠︎" in line for line in output) == 1
    assert any("Zweiter Versuch klappt!" in line for line in output)


# -- typed lines ---------------------------------------------------------------

def test_command_output_prints_but_never_speaks(config):
    spoken = FakeSpeaker()
    # the brain marks command output speakable=False — voice prints it only
    brain = FakeBrain([TurnResult(reply="📊 Dein Fortschritt: …", speakable=False)])

    rc, output = run(config, ["/fortschritt", "q"], brain=brain, speaker=spoken)

    assert rc == 0
    assert any("Dein Fortschritt" in line for line in output)
    assert spoken.spoken == []  # command output is screen text, never spoken
    assert brain.texts == ["/fortschritt"]


def test_typed_plain_text_goes_through_the_brain(config):
    spoken = FakeSpeaker()
    brain = FakeBrain([TurnResult(reply="Ich bin Herr Claw!")])

    rc, output = run(config, ["Hallo, wer bist du?", "q"], brain=brain, speaker=spoken)

    assert rc == 0
    assert brain.texts == ["Hallo, wer bist du?"]
    assert spoken.spoken == ["Ich bin Herr Claw!"]


# -- startup paths -------------------------------------------------------------

def test_default_transcriber_is_wired_into_the_call_site(config, monkeypatch):
    """Regression: run_sprechen must actually USE the default Transcriber it
    builds (the `transcribe` param stays None until it is assigned — a missing
    assignment turned every voice turn into `NoneType is not callable`)."""
    created = []

    class FakeSTT:
        def __init__(self, model):
            self.model = model
            created.append(self)

        def __call__(self, audio):
            return "standardpfad funktioniert"

        def warm(self):
            pass

    monkeypatch.setattr("voice.loop.Transcriber", FakeSTT)
    spoken = FakeSpeaker()
    brain = FakeBrain([TurnResult(reply="Ich habe dich verstanden!")])
    output = []

    rc = run_sprechen(
        config,  # no transcribe injected — the default path under test
        brain=brain,
        speaker=spoken,
        record_audio=lambda: np.zeros(1600, dtype=np.float32),
        key_prompt=FakePrompt(["space", "q"]),
        output_fn=output.append,
    )

    assert rc == 0
    assert created and created[0].model == "base"
    assert any("Du 🗣️: standardpfad funktioniert" in line for line in output)
    assert spoken.spoken == ["Ich habe dich verstanden!"]


def test_warm_failure_warns_but_loop_survives(config, monkeypatch):
    class BrokenSTT:
        def __init__(self, model):
            pass

        def __call__(self, audio):  # pragma: no cover — never reached here
            return ""

        def warm(self):
            raise RuntimeError("offline")

    monkeypatch.setattr("voice.loop.Transcriber", BrokenSTT)
    output = []

    rc = run_sprechen(
        config,
        brain=FakeBrain([TurnResult(reply="OK")]),
        speaker=FakeSpeaker(),
        record_audio=lambda: np.zeros(10, dtype=np.float32),
        key_prompt=FakePrompt(["q"]),
        output_fn=output.append,
    )

    assert rc == 0
    assert any("Whisper-Modell nicht geladen" in line for line in output)


def test_unconfigured_local_fallback_warns_per_turn(monkeypatch, config):
    """Sandbox unavailable + local brain unconfigured (no API key): the loop
    warns honestly on every turn but never crashes — thin clients degrade,
    they don't die (P6)."""
    from dataclasses import replace

    from agent.llm import LLMError

    def broken_client(*args, **kwargs):
        raise LLMError("NVIDIA_API_KEY ist nicht gesetzt.")

    monkeypatch.setattr("agent.turns.make_client", broken_client)
    cfg = replace(config, chat_backend="local")  # no sandbox detour — straight to local
    output = []
    rc = run_sprechen(
        cfg,
        speaker=FakeSpeaker(),
        transcribe=lambda audio: "Hallo",
        record_audio=lambda: np.zeros(1600, dtype=np.float32),
        key_prompt=FakePrompt(["space", "q"]),
        output_fn=output.append,
    )
    assert rc == 0
    assert any("NVIDIA_API_KEY" in line for line in output)


def test_missing_mic_warns_and_typing_still_works(config, monkeypatch):
    class MiclessBridge:
        device = None

        def record(self):
            raise AssertionError("should not record")

    monkeypatch.setattr("voice.loop.Mic", lambda override="": MiclessBridge())
    spoken = FakeSpeaker()
    brain = FakeBrain([TurnResult(reply="Trotzdem da!")])

    rc, output = run(
        config, ["hallo", "q"], brain=brain, speaker=spoken, record_audio=None
    )

    assert rc == 0
    assert any("Kein Eingabemikrofon" in line for line in output)
    assert spoken.spoken == ["Trotzdem da!"]


def test_quit_without_turns_no_farewell(config):
    rc, output = run(config, ["q"])
    assert rc == 0
    assert not any("Tschüss" in line for line in output)


# -- KeyPrompt (terminal line editor) ------------------------------------------

def test_key_prompt_space_records_then_line_submits():
    it = iter([" ", "h", "i", "\r"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    fired = []
    assert prompt.read(on_space=lambda: fired.append(1)) == "hi"
    assert fired == [1]


def test_key_prompt_empty_enter_also_records():
    it = iter(["\r", "q", "\r"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    fired = []
    assert prompt.read(on_space=lambda: fired.append(1)) == "q"
    assert fired == [1]


def test_key_prompt_backspace_edits_line():
    it = iter(["a", "b", "\x7f", "c", "\r"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    assert prompt.read(on_space=lambda: None) == "ac"


def test_key_prompt_ctrl_c_quits():
    it = iter(["\x03"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    assert prompt.read(on_space=lambda: None) is None


def test_key_prompt_eof_quits():
    it = iter([""])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    assert prompt.read(on_space=lambda: None) is None


def test_key_prompt_swallows_escape_sequences():
    it = iter(["\x1b", "[", "D", "o", "k", "\r"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    assert prompt.read(on_space=lambda: None) == "ok"


def test_key_prompt_space_inside_a_word_is_just_text():
    it = iter(["g", "u", "t", " ", "\r"])
    prompt = KeyPrompt(writer=lambda s: None, reader=lambda: next(it))
    assert prompt.read(on_space=lambda: pytest.fail("record fired mid-line")) == "gut "
