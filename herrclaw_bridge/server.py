"""The MCP bridge server — Streamable-HTTP on 127.0.0.1:8765 (SPEC §3/§5).

Exposes exactly seven tools over MCPServer (mcp SDK v2): reminders.*, the
calendar pair, and the scoped vault pair — the ONE Apple/vault door the
sandboxed agent may use (P6: the job bodies run in the sandbox; their
Apple/vault calls cross the bridge here, audited like every other call).
All enforcement stays in the component modules (allowlist + audit) —
this file is glue, deliberately boring: no policy decisions may ever live
in the transport layer.

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


def build_app(vault: Vault | None, *, transport_security=None):
    """ASGI app (for uvicorn); separate from run_server for tests.

    `transport_security` (mcp TransportSecuritySettings) enables the SDK's
    DNS-rebinding protection with an explicit Host/Origin allowlist — used
    when the bridge is reachable under a non-localhost name (the OpenShell
    host alias), where the default host check would 421 every request."""
    return create_server(vault).streamable_http_app(transport_security=transport_security)


def _transport_security_from_env():
    """HERR_BRIDGE_ALLOWED_HOSTS / HERR_BRIDGE_ALLOWED_ORIGINS — comma-separated
    extra Host/Origin values (e.g. `host.openshell.internal:8765`). Unset →
    SDK default (protection off, loopback-only exposure). Set → protection ON
    with loopback plus the listed names."""
    from mcp.server.transport_security import TransportSecuritySettings

    def _split(name):
        return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]

    hosts = _split("HERR_BRIDGE_ALLOWED_HOSTS")
    origins = _split("HERR_BRIDGE_ALLOWED_ORIGINS")
    if not hosts and not origins:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:8765", "localhost:8765", *hosts],
        allowed_origins=["http://127.0.0.1:8765", "http://localhost:8765", *origins],
    )


def vault_from_env() -> Vault | None:
    root = os.environ.get("HERR_VAULT", "").strip()
    return Vault(Path(os.path.expanduser(root))) if root else None


def run_server() -> int:
    import uvicorn

    app = build_app(vault_from_env(), transport_security=_transport_security_from_env())
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
