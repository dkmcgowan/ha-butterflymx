"""Tests for following a call until it ends."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_ENABLE_WEBHOOK,
    DEFAULT_CALL_SCAN_INTERVAL,
    EVENT_CALL,
    EVENT_CALL_ENDED,
    WEBHOOK_FALLBACK_SCAN_INTERVAL,
)
from custom_components.butterflymx.webhook import ButterflyMXWebhookManager

from .conftest import API_URL, BUILDING_ID, call_payload

LAST_CALL_ENTITY = "sensor.unit_4b_last_call"
CALLS_URL = f"{API_URL}/v4/buildings/{BUILDING_ID}/calls"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _call(status: str, logged_at: str) -> dict:
    return {**call_payload(logged_at=logged_at), "status": status}


def _serve(mock_topology, *calls: dict) -> None:
    mock_topology.clear_requests()
    mock_topology.get(CALLS_URL, json={"data": list(calls), "page_info": {"next_page": None}})


async def test_a_call_that_rang_reports_how_it_ended(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The call is read again until its status settles, then ended fires once."""
    await _setup(hass, config_entry)
    rings: list = []
    ended: list = []
    hass.bus.async_listen(EVENT_CALL, rings.append)
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    logged_at = dt_util.utcnow().isoformat()
    calls = config_entry.runtime_data.calls

    _serve(mock_topology, _call("initializing", logged_at))
    await calls.async_refresh()
    await hass.async_block_till_done()
    assert len(rings) == 1
    assert ended == []

    _serve(mock_topology, _call("opened_door", logged_at))
    await calls.async_refresh()
    await calls.async_refresh()
    await hass.async_block_till_done()

    assert len(rings) == 1
    assert len(ended) == 1
    assert ended[0].data["call_id"] == 900001
    assert ended[0].data["status"] == "opened_door"
    assert ended[0].data["resident"] == "Ada Lovelace"
    assert hass.states.get(LAST_CALL_ENTITY).attributes["status"] == "opened_door"


async def test_a_call_already_over_when_first_seen_ends_at_once(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A call that finished before the poll saw it still rings and ends."""
    await _setup(hass, config_entry)
    rings: list = []
    ended: list = []
    hass.bus.async_listen(EVENT_CALL, rings.append)
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)

    _serve(mock_topology, _call("timeout_online_signal", dt_util.utcnow().isoformat()))
    await config_entry.runtime_data.calls.async_refresh()
    await hass.async_block_till_done()

    assert len(rings) == 1
    assert [event.data["status"] for event in ended] == ["timeout_online_signal"]


async def test_an_open_call_is_given_up_on_after_the_window(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A call that never settles stops being followed and never reports ended."""
    await _setup(hass, config_entry)
    ended: list = []
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    old = (dt_util.utcnow() - timedelta(minutes=10)).isoformat()
    calls = config_entry.runtime_data.calls

    _serve(mock_topology, _call("initializing", old))
    await calls.async_refresh()
    _serve(mock_topology, _call("answered", old))
    await calls.async_refresh()
    await hass.async_block_till_done()

    assert ended == []


async def test_push_slowed_poll_speeds_up_while_a_call_is_open(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Nothing is pushed when a call ends, so the log is read at pace until it does."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_ENABLE_WEBHOOK: True})
    with patch.object(ButterflyMXWebhookManager, "async_setup", return_value=True):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    calls = config_entry.runtime_data.calls
    slow = timedelta(seconds=WEBHOOK_FALLBACK_SCAN_INTERVAL)
    assert calls.update_interval == slow
    logged_at = dt_util.utcnow().isoformat()

    _serve(mock_topology, _call("initializing", logged_at))
    await calls.async_refresh()
    assert calls.update_interval == timedelta(seconds=DEFAULT_CALL_SCAN_INTERVAL)

    _serve(mock_topology, _call("declined", logged_at))
    await calls.async_refresh()
    await hass.async_block_till_done()
    assert calls.update_interval == slow
