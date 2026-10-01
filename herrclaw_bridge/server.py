"""The MCP bridge server — Streamable-HTTP on 127.0.0.1:8765 (SPEC §3/§5).

Exposes exactly seven tools over MCPServer (mcp SDK v2): reminders.*, the
calendar pair, and the scoped vault pair. All enforcement stays in the
component modules (allowlist + audit) — this file is glue, deliberately
boring: no policy decisions may ever live in the transport layer.

Run with `herr-claw bridge` (main.py loads the host .env in fallback mode
first; the bridge itself needs no secrets — only HERR_VAULT).
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import calendar_apple, reminders
from .errors import BridgeDenied, BridgeError
from .vault import Vault, VaultDenied

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
DEFAULT_BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/mcp"

INSTRUCTIONS = (
    "Host bridge for Apple Reminders, Apple Calendar and the scoped Obsidian vault. "
    "Everything is allowlisted and audited: reminders → list 'Deutsch' only; "
    "calendar writes → calendar 'Deutsch Lernen' only; vault access → Deutsch/, "
    "'Daily notes'/ and Weeks/ only, append-only. Denials come back as error results."
)


def _guard(fn):
    """Translate policy/ops errors into ToolError so the German denial
    reaches the agent verbatim. Anything else would surface as the SDK's
    opaque 'Error executing tool <name>' (mcp v2 wraps unknown exceptions)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (BridgeError, BridgeDenied, VaultDenied, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def create_server(vault: Vault | None) -> MCPServer:
    server = MCPServer("herrclaw-bridge", instructions=INSTRUCTIONS)

    def require_vault() -> Vault:
        if vault is None:
            raise BridgeError("HERR_VAULT ist nicht gesetzt — Obsidian-Tools sind deaktiviert.")
        return vault

    @server.tool()
    @_guard
    def reminders_list() -> str:
        """Offene Erinnerungen in der Liste 'Deutsch' auflisten."""
        return reminders.list_reminders()

    @server.tool()
    @_guard
    def reminders_add(title: str, due: str = "") -> str:
        """Erinnerung in der Liste 'Deutsch' anlegen (Liste wird bei Bedarf erzeugt).
        due optional: 'YYYY-MM-DD' oder 'YYYY-MM-DD HH:MM'."""
        return reminders.add_reminder(title, due)

    @server.tool()
    @_guard
    def reminders_complete(title: str) -> str:
        """Offene Erinnerungen in 'Deutsch' abschließen, deren Titel `title` enthält."""
        return reminders.complete_reminder(title)

    @server.tool()
    @_guard
    def calendar_freebusy(start: str, end: str) -> str:
        """Termine im Zeitraum als JSON (alle Kalender, nur Lesen).
        start/end: 'YYYY-MM-DD [HH:MM]'."""
        return calendar_apple.freebusy(start, end)

    @server.tool()
    @_guard
    def calendar_add(title: str, start: str, duration_min: int = 15) -> str:
        """Termin im Kalender 'Deutsch Lernen' anlegen — dem einzigen erlaubten Kalender."""
        return calendar_apple.add_event(title, start, duration_min)

    @server.tool()
    @_guard
    def vault_read(rel_path: str) -> str:
        """Datei aus dem Obsidian-Vault lesen (nur Deutsch/, 'Daily notes'/, Weeks/)."""
        return require_vault().read(rel_path)

    @server.tool()
    @_guard
    def vault_append(rel_path: str, text: str) -> str:
        """Text an eine Vault-Datei anhängen (append-only; gleiche Allowlist wie vault_read)."""
        require_vault().append(rel_path, text)
        return f"Angehängt: {rel_path}"

    return server


def build_app(vault: Vault | None):
    """ASGI app (for uvicorn); separate from run_server for tests."""
    return create_server(vault).streamable_http_app()


def vault_from_env() -> Vault | None:
    root = os.environ.get("HERR_VAULT", "").strip()
    return Vault(Path(os.path.expanduser(root))) if root else None


def run_server() -> int:
    import uvicorn

    app = build_app(vault_from_env())
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
