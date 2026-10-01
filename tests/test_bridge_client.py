"""BridgeClient — transport failures surface as German BridgeError."""

import pytest

from agent.bridge import BridgeClient, BridgeError


def test_connection_refused_is_friendly_error():
    client = BridgeClient(url="http://127.0.0.1:1/mcp", timeout_s=3.0)
    with pytest.raises(BridgeError, match="nicht erreichbar|Bridge-Fehler"):
        client.call_tool("reminders_list")


def test_ping_false_when_down():
    assert BridgeClient(url="http://127.0.0.1:1/mcp", timeout_s=3.0).ping() is False
