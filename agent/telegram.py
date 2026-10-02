"""Telegram channel (P4/P5/P6) — sends and the conversation poller over the
allowlisted egress api.telegram.org:443 (SPEC §3/§5). On the host the token
comes from .env (dev fallback/break-glass); in the sandbox the OpenShell
gateway holds the token and resolves it at egress — in-sandbox code only
ever sees the `openshell:resolve:env:` placeholder (cron entry env). The
token is never logged or included in error messages.

Since P6 the conversation receiver lives in the sandbox too: the poller
(agent/poller.py) long-polls getUpdates and grades every incoming message
through the same brain as the TUI. Push stays one-shot per job body.
The bot talks to exactly ONE chat — HERR_TELEGRAM_CHAT_ID (SPEC §3: "the
single allowed user").
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.telegram.org"
CHAT_LIMIT = 4096  # Telegram sendMessage hard limit
SEND_RETRIES = 3

MISSING_TOKEN = "TELEGRAM_BOT_TOKEN ist nicht gesetzt — trage ihn in .env ein (siehe .env.example)."


class TelegramError(RuntimeError):
    """The Bot API is unreachable, misconfigured, or rejected a call."""


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
    """Minimal Bot API client: getMe, sendMessage.

    `_call` retries transient network errors with backoff (same philosophy
    as the NVIDIA path: free-tier/network hiccups clear in seconds); API
    rejections (ok=false) fail fast with Telegram's own description."""

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

    def get_updates(self, offset: int | None = None, timeout_s: float = 30.0) -> list[dict]:
        """Long-poll getUpdates (the conversation receiver inside the
        sandbox). `allowed_updates` pins the subscription to plain messages;
        the offset confirm-and-advance is the caller's job."""
        payload: dict = {"timeout": int(timeout_s), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout_s + 10.0)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: str | int, text: str) -> None:
        for chunk in split_message(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk})


def client_from_env() -> TelegramClient:
    """Build the client from the environment; raises TelegramError with the
    German setup hint when the token is missing."""
    return TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN") or "")
