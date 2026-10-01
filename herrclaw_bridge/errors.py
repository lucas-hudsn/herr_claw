"""Error types for the Apple bridge.

BridgeDenied marks a POLICY denial (allowlist violation — the demo's deny
moment); BridgeError marks an operational failure (app missing, osascript
error, EventKit refused). MCP turns both into error tool results, but the
distinction keeps audit reasons honest: policy denials say why they are
forbidden, failures say what broke.
"""


class BridgeError(RuntimeError):
    """The bridge could not complete an allowed call."""


class BridgeDenied(PermissionError):
    """The call was rejected by the bridge's allowlist — never executed."""
