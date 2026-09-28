"""Tests for what the history sensors show after Home Assistant restarts.

"Last call" and "last door opened" are history. An update or a reboot should not
blank them until the next visitor, and a call that came in while Home Assistant
was down should still land in them, without ringing the doorbell.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.butterflymx.const import EVENT_CALL

from .conftest import API_URL, BUILDING_ID, call_payload, register_topology

LAST_CALL_ENTITY = "sensor.unit_4b_last_call"
LAST_RELEASE_ENTITY = "sensor.unit_4b_last_door_opened"


def _stored(value: str) -> dict:
    """Build what RestoreSensor keeps for a timestamp sensor."""
    return {
        "native_value": {"__type": "<class 'datetime.datetime'>", "isoformat": value},
        "native_unit_of_measurement": None,
    }


async def _setup(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_last_call_survives_a_restart(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """With nothing new in the call log, the call from before the restart stays."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    LAST_CALL_ENTITY,
                    "2026-07-01T09:30:00+00:00",
                    {"call_id": 123, "device_name": "Lobby Panel", "status": "completed",
                     "friendly_name": "not ours to restore"},
                ),
                _stored("2026-07-01T09:30:00+00:00"),
            )
        ],
    )
    register_topology(aioclient_mock)
    await _setup(hass, config_entry)

    state = hass.states.get(LAST_CALL_ENTITY)
    assert state.state == "2026-07-01T09:30:00+00:00"
    assert state.attributes["call_id"] == 123
    assert state.attributes["device_name"] == "Lobby Panel"
    assert state.attributes["friendly_name"] != "not ours to restore"


async def test_last_door_opened_survives_a_restart(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Same for the door release history."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(LAST_RELEASE_ENTITY, "2026-07-02T18:05:00+00:00",
                      {"entry_method": "App call", "release_status": "success"}),
                _stored("2026-07-02T18:05:00+00:00"),
            )
        ],
    )
    register_topology(aioclient_mock)
    await _setup(hass, config_entry)

    state = hass.states.get(LAST_RELEASE_ENTITY)
    assert state.state == "2026-07-02T18:05:00+00:00"
    assert state.attributes["entry_method"] == "App call"


async def test_a_new_call_replaces_the_restored_one(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Once the coordinator has a call of its own, that is what shows."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(LAST_CALL_ENTITY, "2026-07-01T09:30:00+00:00"),
                _stored("2026-07-01T09:30:00+00:00"),
            )
        ],
    )
    register_topology(aioclient_mock)
    await _setup(hass, config_entry)

    aioclient_mock.clear_requests()
    register_topology(aioclient_mock, calls=[call_payload()])
    await config_entry.runtime_data.calls.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(LAST_CALL_ENTITY).state == "2026-08-04T12:00:00+00:00"


async def test_a_call_while_down_is_shown_but_does_not_ring(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """The first poll reaches back far enough to find it, and only seeds."""
    an_hour_ago = (dt_util.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    register_topology(
        aioclient_mock,
        calls=[call_payload(logged_at=an_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ"))],
    )
    events: list = []
    hass.bus.async_listen(EVENT_CALL, events.append)

    await _setup(hass, config_entry)

    assert events == []
    assert hass.states.get(LAST_CALL_ENTITY).state == an_hour_ago.isoformat()


async def test_first_poll_looks_back_a_day_then_polls_normally(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A day and a whole page on startup, then the usual short window."""
    register_topology(aioclient_mock)
    await _setup(hass, config_entry)

    def call_requests():
        return [
            url for method, url, *_ in aioclient_mock.mock_calls
            if method == "GET" and url.path.endswith(f"/buildings/{BUILDING_ID}/calls")
        ]

    first = call_requests()[0]
    assert first.query["per"] == "100"
    # ButterflyMX's own filter format, "YYYY-MM-DD HH:MM:SS UTC"
    since = datetime.strptime(
        first.query["q[logged_at_gteq]"], "%Y-%m-%d %H:%M:%S UTC"
    ).replace(tzinfo=UTC)
    assert dt_util.utcnow() - since > timedelta(hours=23)

    aioclient_mock.clear_requests()
    register_topology(aioclient_mock)
    await config_entry.runtime_data.calls.async_refresh()
    later = call_requests()[0]
    assert later.query["per"] == "20"
    assert str(later).startswith(API_URL)
