"""Tests for setting up the ButterflyMX integration and its entities."""

from __future__ import annotations

from homeassistant.components.lock import (
    DOMAIN as LOCK_DOMAIN,
    SERVICE_OPEN,
    SERVICE_UNLOCK,
    LockState,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    EVENT_CALL,
)

from .conftest import (
    ACCESS_POINTS_RESPONSE,
    ACCOUNTS_URL,
    API_URL,
    BUILDING_ID,
    DEVICES_RESPONSE,
    TENANT_ID,
    TENANTS_RESPONSE,
    call_payload,
    make_token,
)

LOCK_ENTITY = "lock.front_entrance"
SMART_LOCK_ENTITY = "lock.apartment_door"
DOORBELL_ENTITY = "event.unit_4b_doorbell"
LAST_CALL_ENTITY = "sensor.unit_4b_last_call"
SNAPSHOT_ENTITY = "image.unit_4b_last_call_snapshot"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_entities(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Locks, a doorbell, a snapshot and a sensor are created."""
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    for entity_id in (
        LOCK_ENTITY,
        SMART_LOCK_ENTITY,
        DOORBELL_ENTITY,
        LAST_CALL_ENTITY,
        SNAPSHOT_ENTITY,
    ):
        assert hass.states.get(entity_id) is not None, entity_id

    lock = hass.states.get(LOCK_ENTITY)
    assert lock.state == LockState.LOCKED
    assert lock.attributes["assumed_state"] is True
    assert lock.attributes["access_point_id"] == 1001


async def test_unlock_releases_the_door(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Unlocking an access point posts a door release request."""
    mock_topology.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    await _setup(hass, config_entry)

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    release_calls = [
        call
        for call in mock_topology.mock_calls
        if call[1].path.endswith("/door_release_requests")
    ]
    assert len(release_calls) == 1
    assert release_calls[0][2] == {
        "door_release_request": {"tenant_id": TENANT_ID, "access_point_id": 1001}
    }
    assert hass.states.get(LOCK_ENTITY).state == LockState.UNLOCKED


async def test_open_and_unlock_report_the_same_state(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Both release the strike, so neither may claim the door is open.

    Whether anyone actually pushed the door is not something the API reports,
    so ``is_open`` is never set and both actions land on ``unlocked``.
    """
    mock_topology.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    await _setup(hass, config_entry)

    # Two different doors, so the per-door release cooldown cannot quietly
    # swallow the second call and make this pass for the wrong reason.
    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )
    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: SMART_LOCK_ENTITY}, blocking=True
    )

    assert hass.states.get(LOCK_ENTITY).state == LockState.UNLOCKED
    assert hass.states.get(SMART_LOCK_ENTITY).state == LockState.UNLOCKED


async def test_the_door_returns_to_locked_on_its_own(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A released door must go back to locked without anything asking it to.

    Building doors are self-closing: the strike buzzes, someone pushes, the door
    shuts and latches again. Nothing reports that back, so the entity has to
    return to locked by itself or it would sit there claiming to be unlocked and
    the next release would look like a no-op.
    """
    mock_topology.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    # Relock immediately instead of waiting out the real delay.
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(LOCK_ENTITY).state == LockState.LOCKED


async def test_open_uses_device_id_for_unit_lock(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A unit smart lock is opened by device ID."""
    mock_topology.post(f"{API_URL}/v4/door_release_requests", status=201, json={})
    await _setup(hass, config_entry)

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: SMART_LOCK_ENTITY}, blocking=True
    )

    release_calls = [
        call
        for call in mock_topology.mock_calls
        if call[1].path.endswith("/door_release_requests")
    ]
    assert release_calls[0][2]["door_release_request"]["device_id"] == 2002


async def test_new_call_fires_doorbell_and_updates_entities(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A new call fires the doorbell event and refreshes the sensor."""
    await _setup(hass, config_entry)

    events: list = []
    hass.bus.async_listen(EVENT_CALL, events.append)

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/calls",
        json={"data": [call_payload()], "page_info": {"next_page": None}},
    )

    runtime = config_entry.runtime_data
    await runtime.calls.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["call_id"] == 900001
    assert events[0].data["unit_label"] == "4B"

    doorbell = hass.states.get(DOORBELL_ENTITY)
    assert doorbell.attributes["event_type"] == "call"
    assert doorbell.attributes["device_name"] == "Lobby Panel"

    assert hass.states.get(LAST_CALL_ENTITY).state == "2026-08-04T12:00:00+00:00"


async def test_same_call_is_not_announced_twice(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A call already seen is ignored on the next poll."""
    await _setup(hass, config_entry)

    events: list = []
    hass.bus.async_listen(EVENT_CALL, events.append)

    mock_topology.clear_requests()
    mock_topology.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/calls",
        json={"data": [call_payload()], "page_info": {"next_page": None}},
    )

    runtime = config_entry.runtime_data
    await runtime.calls.async_refresh()
    await runtime.calls.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_startup_does_not_replay_old_calls(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Calls that arrived while Home Assistant was down do not ring the doorbell."""
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/access_points", json=ACCESS_POINTS_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/devices", json=DEVICES_RESPONSE)
    aioclient_mock.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/calls",
        json={"data": [call_payload()], "page_info": {"next_page": None}},
    )

    events: list = []
    hass.bus.async_listen(EVENT_CALL, events.append)

    await _setup(hass, config_entry)

    assert events == []
    # The sensor is still seeded so the last call is visible.
    assert hass.states.get(LAST_CALL_ENTITY).state == "2026-08-04T12:00:00+00:00"


async def test_expired_token_is_refreshed_and_saved(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A stale token is renewed during setup and written back to the entry."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_TOKEN: make_token(expires_in=0)},
    )
    mock_topology.post(
        f"{ACCOUNTS_URL}/oauth/token",
        json={
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 86400,
        },
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_TOKEN]["access_token"] == "access-2"
    assert config_entry.data[CONF_TOKEN]["refresh_token"] == "refresh-2"


async def test_reauth_started_when_refresh_is_rejected(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A revoked grant puts the entry into the reauth state."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_TOKEN: make_token(expires_in=0)},
    )
    aioclient_mock.post(f"{ACCOUNTS_URL}/oauth/token", status=401, json={})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_unload(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The entry unloads cleanly."""
    await _setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
