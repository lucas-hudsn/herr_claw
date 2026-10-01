"""LLM client: env handling, TTS sanitizing, correction parsing, Tutor wrapper."""

from types import SimpleNamespace

import pytest

from agent.llm import LLMError, Tutor, make_client, parse_correction, sanitize_reply

SOUL = "Du bist Herr Claw."


def http_503() -> Exception:
    """Minimal stand-in for openai's 503 ResourceExhausted (P0-observed)."""
    from openai import InternalServerError

    response = SimpleNamespace(request=object(), status_code=503, headers={})
    return InternalServerError("503 ResourceExhausted", response=response, body=None)

class TestMakeClient:
    def test_requires_key(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
            make_client()

    def test_builds_client_with_base_url(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "test-key-123")
        client = make_client("https://example.invalid/v1")
        assert str(client.base_url).rstrip("/") == "https://example.invalid/v1"
        assert client.api_key == "test-key-123"


class TestSanitize:
    def test_strips_closed_think_blocks(self):
        assert sanitize_reply("<think>reasoning here</think>\nGuten Tag!") == "Guten Tag!"

    def test_strips_unclosed_think_tail(self):
        assert sanitize_reply("Hallo!<think>ooops") == "Hallo!"

    def test_strips_code_fences_but_keeps_text(self):
        assert sanitize_reply("Vorher\n```python\nprint('hi')\n```\nNachher") == "Vorher\nprint('hi')\nNachher"

    def test_drops_markdown_tables(self):
        text = "Antwort:\n| Wort | Artikel |\n|---|---|\n| Brot | das |"
        assert sanitize_reply(text) == "Antwort:"

    def test_strips_bullets_and_emphasis(self):
        assert sanitize_reply("- **das Brot** — `Bread`") == "das Brot — Bread"

    def test_collapses_blank_runs(self):
        assert sanitize_reply("a\n\n\n\nb") == "a\n\nb"

    def test_empty_after_sanitize(self):
        assert sanitize_reply("<think>x</think>") == ""


class TestParseCorrection:
    def test_plain(self):
        text = "Gut! Richtig: Ich ging zum Bäcker / Deine Version: Ich bin ging zum Bäcker."
        assert parse_correction(text) == (
            "Ich ging zum Bäcker",
            "Ich bin ging zum Bäcker.",
        )

    def test_with_typographic_quotes(self):
        text = "Richtig: „ich ging“ / Deine Version: „ich bin ging“"
        assert parse_correction(text) == ("ich ging", "ich bin ging")

    def test_no_match(self):
        assert parse_correction("Alles richtig, super gemacht!") is None

    def test_multiline_template_matches_too(self):
        # models often render the template on two lines (seen live in P1 smoke test)
        text = "Richtig: Ich ging zum Bäcker\nDeine Version: Ich bin ging zum Bäcker."
        assert parse_correction(text) == ("Ich ging zum Bäcker", "Ich bin ging zum Bäcker.")

    def test_richtig_alone_does_not_match(self):
        assert parse_correction("Richtig: ich ging — super!") is None


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(_FakeMessage(content))]


class _FakeCompletions:
    def __init__(self, content, seen=None, errors: list[Exception] | None = None):
        self._content = content
        self._seen = seen
        self._errors = list(errors or [])
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._seen is not None:
            self._seen.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)
        return _FakeResponse(self._content)


class TestTutor:
    @staticmethod
    def _client(completions):
        client = type("Client", (), {})()
        client.chat = type("Chat", (), {})()
        client.chat.completions = completions
        return client

    def test_reply_sanitizes_and_sends_system_prompt(self):
        seen = []
        completions = _FakeCompletions("<think>hmm</think>Hallo, **Lucas**!", seen=seen)
        tutor = Tutor(self._client(completions), model="m", system_prompt=SOUL)
        reply = tutor.reply([{"role": "user", "content": "Hi"}])
        assert reply == "Hallo, Lucas!"
        assert seen[0]["messages"][0]["role"] == "system"
        assert seen[0]["messages"][0]["content"] == SOUL
        assert seen[0]["model"] == "m"

    def test_thinking_disabled_per_p0(self):
        seen = []
        completions = _FakeCompletions("Antwort", seen=seen)
        Tutor(self._client(completions), model="m", system_prompt=SOUL).reply([])
        extra = seen[0]["extra_body"]
        assert extra["chat_template_kwargs"]["thinking"] is False

    def test_retries_transient_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("agent.llm.time.sleep", lambda s: None)
        completions = _FakeCompletions("Antwort", errors=[http_503()])
        tutor = Tutor(self._client(completions), model="m", system_prompt=SOUL)
        assert tutor.reply([]) == "Antwort"
        assert completions.calls == 2  # failed once, retried, succeeded

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setattr("agent.llm.time.sleep", lambda s: None)
        errors = [http_503() for _ in range(9)]
        completions = _FakeCompletions("Antwort", errors=errors)
        tutor = Tutor(self._client(completions), model="m", system_prompt=SOUL)
        with pytest.raises(LLMError, match="InternalServerError"):
            tutor.reply([])
        assert completions.calls == 3  # MAX_ATTEMPTS, no infinite retry

    def test_non_transient_error_fails_fast(self):
        completions = _FakeCompletions("", errors=[RuntimeError("boom")])
        tutor = Tutor(self._client(completions), model="m", system_prompt=SOUL)
        with pytest.raises(LLMError, match="RuntimeError"):
            tutor.reply([])
        assert completions.calls == 1

    def test_system_note_appended(self):
        seen = []
        completions = _FakeCompletions("Erklärung", seen=seen)
        Tutor(self._client(completions), model="m", system_prompt=SOUL).reply(
            [{"role": "user", "content": "q"}], system_note="Antworte auf Englisch."
        )
        assert "Antworte auf Englisch." in seen[0]["messages"][0]["content"]

    def test_empty_content_rejected(self):
        completions = _FakeCompletions("<think>only reasoning</think>")
        with pytest.raises(LLMError, match="leer"):
            Tutor(self._client(completions), model="m", system_prompt=SOUL).reply([])
