"""Anchoring settings (P2-A, 09 §4).

`anchor_target` is the enum, never a bare string: a typo in the environment must
fail at startup, not at insert time inside the worker — where it would surface
only as a silent no-anchor hours later (09 §4).
"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.enums import AnchorTarget


def test_anchor_defaults_are_dev_safe():
    s = Settings()
    assert s.anchor_enabled is True
    assert s.anchor_target is AnchorTarget.opentimestamps
    assert s.anchor_interval_seconds > 0
    # Bitcoin confirmation takes hours; polling for the upgrade faster is waste.
    assert s.anchor_upgrade_interval_seconds >= s.anchor_interval_seconds
    assert s.anchor_max_rows > 0
    assert s.ots_timeout_seconds > 0


def test_anchor_target_rejects_an_unknown_value():
    with pytest.raises(ValidationError):
        Settings(anchor_target="bitcoin-mainnet")


def test_anchor_target_accepts_the_documented_fallback():
    # `polygon` is a reserved fallback (06 §9): the value parses, no code path
    # consumes it in Phase 2.
    assert Settings(anchor_target="polygon").anchor_target is AnchorTarget.polygon


def test_calendar_urls_split_into_a_list():
    s = Settings(ots_calendar_urls="https://a.example, https://b.example ")
    assert s.calendar_urls == ["https://a.example", "https://b.example"]


def test_calendar_urls_default_is_not_empty():
    assert len(Settings().calendar_urls) >= 1


@pytest.mark.parametrize("value", ["", "   ", " , , "])
def test_calendar_urls_rejects_an_empty_configuration(value):
    # Anchoring with no calendar to submit to is a misconfiguration, not a
    # degraded mode — fail at startup.
    with pytest.raises(ValidationError):
        Settings(ots_calendar_urls=value)
