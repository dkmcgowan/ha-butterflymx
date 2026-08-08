"""Tests for the door release log.

Covers every way a door gets opened, not just releases sent from here: a PIN at
the keypad, answering the intercom in the app, or this integration.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import EVENT_DOOR_RELEASE
from custom_components.butterflymx.models import AccessLogEntry, AccessTool

from .conftest import (
    ACCESS_POINT_ID,
    ACCESS_TOOL_ID,
    API_URL,
    BUILDING_ID,
    TENANT_ID,
    access_log_payload,
)

RELEASE_ENTITY = "event.unit_4b_door_opened"
LAST_RELEASE_ENTITY = "sensor.unit_4b_last_door_opened"


@pytest.mark.parametrize(
    ("raw_method", "expected_method", "expected_tool"),
    [
        # Every form the live API was seen to use.
        ("App call", "App call", None),
        ("Swipe to open", "Swipe to open", None),
        ("API", "API", None),
        ({"access_tool": ACCESS_TOOL_ID}, "access_tool", ACCESS_TOOL_ID),
    ],
    ids=["app-call", "swipe", "api", "access-tool"],
)
def test_entry_method_arrives_in_more_than_one_shape(
    raw_method, expected_method, expected_tool
) -> None:
    """Sometimes a phrase, sometimes an object naming what was used."""
    entry = AccessLogEntry.from_api(access_log_payload(entry_method=raw_method))

    assert entry is not None
    assert entry.entry_method == expected_method
    assert entry.access_tool_id == expected_tool


def test_the_resident_name_is_not_kept() -> None:
    """Entries carry a name. Nothing here needs it, so nothing reads it."""
    entry = AccessLogEntry.from_api(access_log_payload())

    assert entry is not None
    assert "Lovelace" not in repr(entry)


def test_an_entry_with_no_id_is_dropped() -> None:
    """Without an ID it cannot be deduplicated, so it cannot be trusted."""
    payload = access_log_payload()
    del payload["id"]

    assert AccessLogEntry.from_api(payload) is None


async def test_a_door_opening_fires_an_event(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A release that appears after startup is announced."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    events: list = []
    hass.bus.async_listen(EVENT_DOOR_RELEASE, events.append)

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json={"data": [access_log_payload()], "page_info": {"next_page": None}},
    )
    await config_entry.runtime_data.access_log.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["access_point_id"] == ACCESS_POINT_ID
    assert data["release_type"] == "Tenant"
    assert data["tenant_id"] == TENANT_ID
    assert data["unit_label"]

    # The event entity should have fired too, not just the bus event.
    assert hass.states.get(RELEASE_ENTITY) is not None


async def test_startup_does_not_announce_old_openings(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A door opened before Home Assistant started is history, not news.

    The access log already holds an entry when setup runs, and the first poll
    must record it as seen rather than announce it.
    """
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json={"data": [access_log_payload()], "page_info": {"next_page": None}},
    )

    events: list = []
    config_entry.add_to_hass(hass)
    hass.bus.async_listen(EVENT_DOOR_RELEASE, events.append)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert events == []


def test_an_access_tool_never_keeps_the_code() -> None:
    """The payload carries the live PIN. Nothing here may hold on to it.

    A field that is never read cannot leak, which is the whole reason the model
    takes the ID and the type and leaves the rest.
    """
    tool = AccessTool.from_api(
        {"id": ACCESS_TOOL_ID, "type": "pin", "code": "131619", "building_id": 1}
    )

    assert tool is not None
    assert tool.label == "PIN"
    assert "131619" not in repr(tool)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"id": 1, "type": "pin"}, "PIN"),
        ({"id": 1, "type": "rfid_tag"}, "Fob"),
        ({"id": 1, "type": "rfid_tag", "name": "Dog walker"}, "Dog walker"),
        # A type nobody has seen still reads better than an ID.
        ({"id": 1, "type": "mobile_pass"}, "Mobile pass"),
        ({"id": 1}, "Access tool 1"),
    ],
    ids=["pin", "fob", "named", "unknown-type", "no-type"],
)
def test_an_access_tool_describes_itself(payload: dict, expected: str) -> None:
    """Whatever the tool is, the log should say it in words."""
    tool = AccessTool.from_api(payload)

    assert tool is not None and tool.label == expected


async def test_a_pin_entry_says_pin_rather_than_an_id(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The common case, and the reason access tools are read at all.

    Nine openings in ten arrive as ``{"access_tool": 8432576}``, which says
    nothing on its own. The ID stays alongside for automations that want to be
    exact.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    events: list = []
    hass.bus.async_listen(EVENT_DOOR_RELEASE, events.append)

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json={
            "data": [access_log_payload(entry_method={"access_tool": ACCESS_TOOL_ID})],
            "page_info": {"next_page": None},
        },
    )
    await config_entry.runtime_data.access_log.async_refresh()
    await hass.async_block_till_done()

    assert events[0].data["entry_method"] == "PIN"
    assert events[0].data["access_tool_id"] == ACCESS_TOOL_ID
    assert hass.states.get(LAST_RELEASE_ENTITY).attributes["entry_method"] == "PIN"


async def test_an_unknown_tool_keeps_the_raw_method(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A fob added since the last topology refresh must not break the event."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json={
            "data": [access_log_payload(entry_method={"access_tool": 999999})],
            "page_info": {"next_page": None},
        },
    )
    await config_entry.runtime_data.access_log.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(LAST_RELEASE_ENTITY)
    assert state.attributes["entry_method"] == "access_tool"
    assert state.attributes["access_tool_id"] == 999999


async def test_the_sensor_reports_the_last_opening(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The sensor is the timestamp, with the details as attributes."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json={
            "data": [access_log_payload(entry_method={"access_tool": ACCESS_TOOL_ID})],
            "page_info": {"next_page": None},
        },
    )
    await config_entry.runtime_data.access_log.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(LAST_RELEASE_ENTITY)
    assert state is not None
    assert state.state == "2026-08-04T12:00:00+00:00"
    assert state.attributes["access_point_id"] == ACCESS_POINT_ID
    assert state.attributes["entry_method"] == "PIN"
    assert state.attributes["access_tool_id"] == ACCESS_TOOL_ID
