"""In-process bridge for host-side job execution (P5).

When the OpenClaw cron in the sandbox triggers a §4.1 job, the MCP tool
`run_job` executes the job body INSIDE the bridge process. The job code
talks to Apple/vault through the same BridgeClient.call_tool seam as
always — LocalBridge is that seam without the HTTP loopback: it dispatches
to the exact same allowlisted component functions the MCP tools call, so
allowlist + audit enforcement stays in one place (AGENTS.md).

Never used inside the sandbox — the sandbox only ever reaches the bridge
over its allowlisted endpoint.
"""

from __future__ import annotations

from . import calendar_apple, reminders
from .errors import BridgeError
from .vault import Vault


class LocalBridge:
    """call_tool-compatible dispatcher over the bridge component modules."""

    def __init__(self, vault: Vault | None) -> None:
        self.vault = vault

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        args = arguments or {}
        if name == "reminders_list":
            return reminders.list_reminders()
        if name == "reminders_add":
            return reminders.add_reminder(args["title"], args.get("due", ""))
        if name == "reminders_complete":
            return reminders.complete_reminder(args["title"])
        if name == "calendar_freebusy":
            return calendar_apple.freebusy(args["start"], args["end"])
        if name == "calendar_add":
            return calendar_apple.add_event(
                args["title"], args["start"], duration_min=args.get("duration_min", 15)
            )
        if name == "vault_read":
            return self._require_vault().read(args["rel_path"])
        if name == "vault_append":
            self._require_vault().append(args["rel_path"], args["text"])
            return f"Angehängt: {args['rel_path']}"
        raise BridgeError(f"Unbekanntes Bridge-Tool „{name}“.")

    def _require_vault(self) -> Vault:
        if self.vault is None:
            raise BridgeError("HERR_VAULT ist nicht gesetzt — Obsidian-Tools sind deaktiviert.")
        return self.vault
