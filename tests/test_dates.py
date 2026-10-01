"""dates.parse_local — local-time ISO parsing for the Apple bridge."""

from datetime import datetime

import pytest

from herrclaw_bridge.dates import parse_local
from herrclaw_bridge.errors import BridgeError


def test_date_only_gets_default_time():
    assert parse_local("2026-10-02") == datetime(2026, 10, 2, 9, 0)


def test_datetime_with_space():
    assert parse_local("2026-10-02 14:30") == datetime(2026, 10, 2, 14, 30)


def test_datetime_with_t_separator():
    assert parse_local("2026-10-02T08:15") == datetime(2026, 10, 2, 8, 15)


def test_timezone_aware_becomes_local_naive():
    parsed = parse_local("2026-10-02T14:30+02:00")
    assert parsed.tzinfo is None
    assert parsed == datetime(2026, 10, 2, 14, 30)


@pytest.mark.parametrize("bad", ["", "   ", "gestern", "2026-13-40", "2026/10/02"])
def test_garbage_is_bridgeerror(bad):
    with pytest.raises(BridgeError):
        parse_local(bad)
