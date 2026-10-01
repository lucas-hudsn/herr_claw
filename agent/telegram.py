"""Telegram channel (P4) — direct Bot API over the allowlisted egress
api.telegram.org:443 (SPEC §3/§5). This is the host-fallback path: in
sandbox mode the OpenShell gateway holds the token and bridges the same
chat (P0 onboard); on the host the token comes from .env via the
environment. The token is never logged or included in error messages.

The bot answers exactly ONE chat — HERR_TELEGRAM_CHAT_ID (SPEC §3: "the
single allowed user"). Other chats are reported to the caller and ignored,
never answered.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API_BASE = "https://api.telegram.org"
CHAT_LIMIT = 4096  # Telegram sendMessage hard limit
POLL_CAP_S = 50  # long-poll ceiling — keeps job timers responsive
SEND_RETRIES = 3

MISSING_TOKEN = "TELEGRAM_BOT_TOKEN ist nicht gesetzt — trage ihn in .env ein (siehe .env.example)."


class TelegramError(RuntimeError):
    """The Bot API is unreachable, misconfigured, or rejected a call."""


@dataclass(frozen=True)
class Incoming:
    update_id: int
    chat_id: str
    text: str


def parse_update(update: dict) -> Incoming | None:
    """Extract (update_id, chat_id, text); None for edits, non-text messages
    and anything without a chat — those are simply ignored."""
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    if not text or "id" not in chat or "update_id" not in update:
        return None
    return Incoming(update_id=int(update["update_id"]), chat_id=str(chat["id"]), text=text)


def split_message(text: str, limit: int = CHAT_LIMIT) -> list[str]:
    """Split at newline boundaries under the Telegram limit; hard-split a
    single overlong line (keeps LLM replies deliverable no matter what)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


class TelegramClient:
    """Minimal Bot API client: getMe, getUpdates (long poll), sendMessage.

    `_call` retries transient network errors with backoff (same philosophy
    as the NVIDIA path: free-tier/network hiccups clear in seconds); API
    rejections (ok=false) fail fast with Telegram's own description — a 409
    means another process is polling with this token."""

    def __init__(
        self,
        token: str,
        base_url: str = API_BASE,
        opener=None,  # injectable transport: (request, timeout) → file-like
    ) -> None:
        token = (token or "").strip()
        if not token:
            raise TelegramError(MISSING_TOKEN)
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen

    def _call(self, method: str, payload: dict | None = None, timeout: float = 35.0) -> dict:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        last_exc: Exception | None = None
        for attempt in range(SEND_RETRIES):
            try:
                with self._opener(request, timeout=timeout) as response:
                    data = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                # HTTP errors (401/409/…) will not get better on retry — fail fast
                detail = exc.reason if isinstance(exc.reason, str) else ""
                if exc.code == 409:
                    raise TelegramError(
                        "Telegram: 409 Conflict — läuft schon ein anderer Prozess "
                        "mit diesem Bot-Token?"
                    ) from exc
                raise TelegramError(f"Telegram-API-Fehler ({exc.code}){(': ' + detail) if detail else ''}.") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt == SEND_RETRIES - 1:
                    raise TelegramError(
                        "Keine Verbindung zu api.telegram.org — Netzwerk prüfen."
                    ) from exc
                time.sleep(2**attempt)
        if not data.get("ok"):
            raise TelegramError(f"Telegram-API: {data.get('description') or 'unbekannter Fehler'}")
        return data.get("result")

    def get_me(self) -> dict:
        return self._call("getMe") or {}

    def get_updates(self, offset: int, timeout_s: int = POLL_CAP_S) -> list[dict]:
        """Long poll. `offset` = last seen update_id + 1 (0 = from the start)."""
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": max(0, min(POLL_CAP_S, int(timeout_s)))},
            timeout=max(5.0, float(timeout_s) + 15.0),
        )
        return result or []

    def send_message(self, chat_id: str | int, text: str) -> None:
        for chunk in split_message(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk})


def client_from_env() -> TelegramClient:
    """Build the host-fallback client from the environment; raises
    TelegramError with the German setup hint when the token is missing."""
    import os

    return TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN") or "")
