"""voice.tts — markdown/emoji stripping and the `say -v Anna` invocation."""

from voice.tts import DEFAULT_VOICE, Speaker, speech_text


def test_speech_text_strips_markdown_emoji_and_reasoning():
    raw = (
        "<think>internal reasoning</think>"
        "**Hallo** Lucas!\n"
        "- Punkt eins\n"
        "- Punkt zwei\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "🎉 Bis bald!"
    )
    spoken = speech_text(raw)
    assert "Hallo Lucas!" in spoken
    assert "Punkt eins" in spoken and "Punkt zwei" in spoken
    assert "|" not in spoken
    assert "**" not in spoken
    assert "🎉" not in spoken
    assert "internal" not in spoken


def test_speech_text_keeps_plain_german():
    assert speech_text("Das Brot (die Brote) schmeckt.") == "Das Brot (die Brote) schmeckt."


def test_speaker_runs_say_with_pinned_voice_and_clean_text():
    calls = []
    speaker = Speaker(runner=lambda argv: calls.append(argv))
    speaker.say("Sehr **gut**! 🎉")
    assert calls == [["say", "-v", "Anna", "Sehr gut!"]]
    assert DEFAULT_VOICE == "Anna"  # pinned by AGENTS.md


def test_speaker_skips_empty_output():
    calls = []
    speaker = Speaker(runner=lambda argv: calls.append(argv))
    speaker.say("   ")
    speaker.say("| |")
    assert calls == []


def test_say_availability_is_a_bool():
    assert isinstance(Speaker.available(), bool)
