"""Sync MCP client to the host bridge (the ONLY Apple/vault path for agent code).

Per AGENTS.md the tutor never touches osascript/EventKit directly — it calls
the bridge over MCP, so allowlist + audit enforcement live in exactly one
place (herrclaw_bridge). The SDK client is async; the chat loop is sync, so
this wrapper runs one asyncio event loop per call (fine at bridge-call
frequency — a handful per session).

Tool errors (policy denials included) arrive as is_error results with a
German message; they are re-raised as BridgeError so callers can degrade
honestly instead of pretending the booking happened.
"""

from __future__ import annotations

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from herrclaw_bridge.server import DEFAULT_BRIDGE_URL


class BridgeError(RuntimeError):
    """The bridge is unreachable, or the call failed/denied on its side."""


class BridgeClient:
    def __init__(self, url: str = DEFAULT_BRIDGE_URL, timeout_s: float = 30.0) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        try:
            return asyncio.run(asyncio.wait_for(self._call(name, arguments or {}), self.timeout_s))
        except BridgeError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise BridgeError(
                f"Bridge-Timeout nach {self.timeout_s:.0f}s ({self.url}) — läuft `herr-claw bridge`?"
            )
        except BaseException as exc:  # anyio raises ExceptionGroup on refused connects
            raise BridgeError(_transport_message(exc, self.url)) from exc

    def ping(self) -> bool:
        try:
            self.call_tool("reminders_list")
            return True
        except BridgeError:
            return False

    async def _call(self, name: str, arguments: dict) -> str:
        async with streamable_http_client(self.url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        if getattr(result, "is_error", False):
            raise BridgeError(_result_text(result) or "Bridge-Aufruf fehlgeschlagen.")
        return _result_text(result)


def _result_text(result) -> str:
    parts = [getattr(item, "text", "") for item in (result.content or [])]
    return "\n".join(p for p in parts if p).strip()


def _transport_message(exc: BaseException, url: str) -> str:
    for err in _flatten(exc):
        text = str(err).lower()
        if "connection refused" in text or "connect" in text and "errno 61" in text:
            return f"Bridge nicht erreichbar ({url}) — starte `herr-claw bridge`."
        if "timeout" in text or "timed out" in text:
            return f"Bridge-Zeitüberschreitung ({url}) — läuft `herr-claw bridge`?"
    return f"Bridge-Fehler ({type(exc).__name__}) — läuft `herr-claw bridge`?"


def _flatten(exc: BaseException):
    for sub in getattr(exc, "exceptions", ()) or ():
        yield from _flatten(sub)
    yield exc
