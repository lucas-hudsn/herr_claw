"""agent/telegram.py — Bot API client: retries, chat allowlist, chunking.

The fake transport replaces urllib.request.urlopen; no network, no secrets.
"""

import contextlib
import json
import urllib.error

import pytest

from agent.telegram import (
    CHAT_LIMIT,
    TelegramClient,
    TelegramError,
    parse_update,
    split_message,
)


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Returns `payload`; raises URLError for the first `fail_times` calls."""

    def __init__(self, payload, fail_times=0):
        self.payload = payload
        self.fail_times = fail_times
        self.calls = 0
        self.requests = []

    def __call__(self, request, timeout):
        self.calls += 1
        self.requests.append((request, timeout))
        if self.calls <= self.fail_times:
            raise urllib.error.URLError("connection reset")
        return FakeResponse(self.payload)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("agent.telegram.time.sleep", lambda s: None)


def test_missing_token_is_a_german_setup_error():
    with pytest.raises(TelegramError) as excinfo:
        TelegramClient("")
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_get_me_roundtrip():
    opener = FakeOpener({"ok": True, "result": {"id": 1, "username": "frau_claw_bot"}})
    client = TelegramClient("t0k3n", opener=opener)
    assert client.get_me()["username"] == "frau_claw_bot"
    request, _ = opener.requests[0]
    assert request.full_url.endswith("/bott0k3n/getMe")


def test_transient_network_errors_are_retried_with_backoff():
    opener = FakeOpener({"ok": True, "result": {"id": 1}}, fail_times=2)
    client = TelegramClient("t", opener=opener)
    assert client.get_me() == {"id": 1}
    assert opener.calls == 3  # 2 failures, then success (SEND_RETRIES)


def test_persistent_network_failure_raises_german_error():
    opener = FakeOpener({"ok": True, "result": {}}, fail_times=99)
    client = TelegramClient("sekret-token-123", opener=opener)
    with pytest.raises(TelegramError) as excinfo:
        client.get_me()
    assert "api.telegram.org" in str(excinfo.value)
    assert "sekret-token-123" not in str(excinfo.value)  # the token never leaks


def test_http_409_names_the_real_cause():
    def conflict(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", hdrs=None, fp=None
        )

    client = TelegramClient("t", opener=conflict)
    with pytest.raises(TelegramError) as excinfo:
        client.get_me()
    assert "409" in str(excinfo.value) and "anderer Prozess" in str(excinfo.value)


def test_api_rejection_surfaces_description():
    opener = FakeOpener({"ok": False, "description": "Bad Request: chat not found"})
    client = TelegramClient("t", opener=opener)
    with pytest.raises(TelegramError) as excinfo:
        client.send_message("1", "hi")
    assert "chat not found" in str(excinfo.value)


def test_get_updates_sends_offset_and_timeout():
    opener = FakeOpener({"ok": True, "result": [{"update_id": 7}]})
    client = TelegramClient("t", opener=opener)
    updates = client.get_updates(offset=5, timeout_s=42)
    assert updates == [{"update_id": 7}]
    request, timeout = opener.requests[0]
    body = json.loads(request.data)
    assert body == {"offset": 5, "timeout": 42}
    assert timeout >= 42  # client-side socket timeout exceeds the long poll


def test_get_updates_timeout_is_capped():
    opener = FakeOpener({"ok": True, "result": []})
    client = TelegramClient("t", opener=opener)
    client.get_updates(offset=0, timeout_s=10_000)
    assert json.loads(opener.requests[0][0].data)["timeout"] == 50


def test_send_message_chunks_over_the_limit():
    opener = FakeOpener({"ok": True, "result": {"message_id": 1}})
    client = TelegramClient("t", opener=opener)
    text = "\n".join(f"Zeile {i}" for i in range(1000))  # > 4096 chars
    client.send_message("42", text)
    assert opener.calls > 1
    for request, _ in opener.requests:
        body = json.loads(request.data)
        assert body["chat_id"] == "42"
        assert len(body["text"]) <= CHAT_LIMIT
    assert "".join(json.loads(r.data)["text"] for r, _ in opener.requests).startswith("Zeile 0")


def test_split_message_hard_splits_single_long_line():
    chunks = split_message("x" * (CHAT_LIMIT + 10))
    assert len(chunks) == 2
    assert sum(len(c) for c in chunks) == CHAT_LIMIT + 10


def test_split_message_empty_is_noop():
    assert split_message("") == []


# ---- update parsing ----------------------------------------------------------


def test_parse_update_extracts_text_chat_and_id():
    update = {"update_id": 3, "message": {"text": "/quiz", "chat": {"id": 99}}}
    incoming = parse_update(update)
    assert incoming.update_id == 3
    assert incoming.chat_id == "99"
    assert incoming.text == "/quiz"


def test_parse_update_ignores_non_text_and_broken_shapes():
    assert parse_update({"update_id": 1}) is None
    assert parse_update({"update_id": 2, "message": {"chat": {"id": 1}}}) is None
    assert parse_update({"update_id": 3, "message": {"text": "hi"}}) is None
