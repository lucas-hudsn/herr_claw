"""NVIDIA Nemotron inference via the OpenAI-compatible endpoint.

NVIDIA_API_KEY comes from the environment only (host .env in fallback mode
via python-dotenv). It is never logged, echoed, or given a hardcoded fallback.

P0 verdicts baked in here (SPEC §3):
- `thinking: False` on every call — reasoning mode measured 9–54s vs ~1.4s,
  and the <5s voice target is only reachable with it disabled.
- Retry with backoff on transient failures (free-tier 503 ResourceExhausted
  observed in P0) — auth/config errors fail fast.

Replies are sanitized for TTS: reasoning blocks and markdown stripped —
LLM output must stay short and speakable (AGENTS.md).
"""

from __future__ import annotations

import os
import re
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .config import DEFAULT_BASE_URL

DEFAULT_TIMEOUT_S = 90.0
DEFAULT_MAX_TOKENS = 2048
MAX_ATTEMPTS = 3  # 3 tries, ~1s + ~2s backoff — P0 observed at most one 503

_THINKING_OFF_EXTRA_BODY = {"chat_template_kwargs": {"thinking": False}}

_THINK_CLOSED = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n?(.*?)```", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_LIST_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*\*|__|`")
_BLANK_RUN = re.compile(r"\n{3,}")


class LLMError(RuntimeError):
    """Inference is not configured or the call failed."""


def make_client(base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT_S) -> OpenAI:
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise LLMError(
            "NVIDIA_API_KEY ist nicht gesetzt — trage sie in .env ein (siehe .env.example)."
        )
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def sanitize_reply(text: str) -> str:
    """Strip reasoning blocks and markdown so the reply is speakable."""
    text = _THINK_CLOSED.sub("", text)
    text = _THINK_UNCLOSED.sub("", text)
    text = _CODE_FENCE.sub(lambda m: m.group(1).strip(), text)
    text = _TABLE_ROW.sub("", text)
    text = _LIST_BULLET.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


_CORRECTION_SLASH_RE = re.compile(
    r"Richtig:\s*(?P<fix>.+?)\s*/\s*Deine Version:\s*(?P<example>.+)"
)
_CORRECTION_LINES_RE = re.compile(
    r"Richtig:\s*(?P<fix>[^\n]+?)\s*\n\s*Deine Version:\s*(?P<example>[^\n]+)"
)
_QUOTE_CHARS = "„“\"»«'`"


def parse_correction(text: str) -> tuple[str, str] | None:
    """Extract (fix, example) from the SOUL correction template
    'Richtig: … / Deine Version: …'. Accepts the canonical one-line form
    and the two-line rendering models sometimes produce; else None."""
    match = _CORRECTION_SLASH_RE.search(text) or _CORRECTION_LINES_RE.search(text)
    if not match:
        return None
    fix = match.group("fix").strip().strip(_QUOTE_CHARS)
    example = match.group("example").strip().strip(_QUOTE_CHARS)
    if not fix or not example:
        return None
    return fix, example


def _friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    if "Authentication" in name or "Permission" in name:
        return "Der NVIDIA-API-Schlüssel ist ungültig (NVIDIA_API_KEY)."
    if "RateLimit" in name:
        return "NVIDIA-API: Rate-Limit erreicht — gleich nochmal versuchen."
    if "Connection" in name or "Timeout" in name:
        return "Keine Verbindung zu integrate.api.nvidia.com — Netzwerk prüfen."
    return f"NVIDIA-Inferenz fehlgeschlagen ({name})."


class Tutor:
    """Thin wrapper: system prompt (SOUL) + rolling history → sanitized reply."""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        system_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens

    def reply(self, window: list[dict[str, str]], system_note: str | None = None) -> str:
        system = self.system_prompt
        if system_note:
            system = f"{system}\n\n{system_note}"
        messages = [{"role": "system", "content": system}, *window]
        response = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.6,
                    top_p=0.95,
                    max_tokens=self.max_tokens,
                    extra_body=dict(_THINKING_OFF_EXTRA_BODY),
                )
                break
            except (InternalServerError, RateLimitError, APIConnectionError, APITimeoutError) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise LLMError(_friendly_error(exc)) from exc
                time.sleep(2**attempt)  # 1s, 2s — free-tier 503 clears fast
            except Exception as exc:
                raise LLMError(_friendly_error(exc)) from exc
        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""
        reply = sanitize_reply(content)
        if not reply:
            raise LLMError("Das Modell hat eine leere Antwort geschickt.")
        return reply
