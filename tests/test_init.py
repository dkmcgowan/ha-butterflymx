"""Tests for setting up the ButterflyMX integration and its entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.lock import (
    DOMAIN as LOCK_DOMAIN,
    SERVICE_OPEN,
    SERVICE_UNLOCK,
    LockState,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_ENABLE_WEBHOOK,
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    DEFAULT_CALL_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CALL,
    WEBHOOK_FALLBACK_SCAN_INTERVAL,
)
from custom_components.butterflymx.webhook import ButterflyMXWebhookManager

from .conftest import (
    ACCOUNTS_URL,
    API_URL,
    BUILDING_ID,
    TENANT_ID,
    call_payload,
    make_token,
    register_topology,
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


async def test_the_doorbell_declares_the_ring_event_type(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Home Assistant requires "ring" on anything with the doorbell device class.

    Without it, setup logs a warning naming this repository and the entity stops
    working in Home Assistant 2027.4. Found by installing it, not by any test,
    which is why there is one now.
    """
    await _setup(hass, config_entry)

    doorbell = hass.states.get(DOORBELL_ENTITY)
    assert doorbell is not None
    assert doorbell.attributes["device_class"] == "doorbell"
    assert "ring" in doorbell.attributes["event_types"]


async def test_doors_and_the_unit_hang_off_the_building(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The building device has to exist, or the parent links point at nothing.

    Nothing lives on a building, so no entity creates it and setup has to. A
    ``via_device`` naming a device that was never registered is reported by
    Home Assistant as an error, and the grouping silently does not happen.
    """
    await _setup(hass, config_entry)
    registry = dr.async_get(hass)

    building = registry.async_get_device(
        identifiers={(DOMAIN, f"tenant_{TENANT_ID}_building_{BUILDING_ID}")}
    )
    assert building is not None
    assert building.name == "Crimson"

    for identifier in (
        f"tenant_{TENANT_ID}_access_point_1001",
        f"tenant_{TENANT_ID}",
    ):
        device = registry.async_get_device(identifiers={(DOMAIN, identifier)})
        assert device is not None, identifier
        assert device.via_device_id == building.id, identifier


async def test_identifiers_carry_the_tenancy(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """A second ButterflyMX login in the same building must not collide.

    Home Assistant gives each config entry its own devices, so nothing here may
    be identified by the door alone.
    """
    await _setup(hass, config_entry)
    registry = dr.async_get(hass)

    assert registry.async_get_device(identifiers={(DOMAIN, "access_point_1001")}) is None
    assert registry.async_get_device(identifiers={(DOMAIN, "building_777")}) is None

    entity_registry = er.async_get(hass)
    lock = entity_registry.async_get(LOCK_ENTITY)
    assert lock is not None
    assert lock.unique_id == f"{DOMAIN}_tenant_{TENANT_ID}_access_point_1001"


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
    """A door not reachable as an access point is opened by device ID."""
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
    assert doorbell.attributes["event_type"] == "ring"
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
    register_topology(aioclient_mock, calls=[call_payload()])

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


async def _setup_with_webhook(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Set the entry up with webhook push turned on."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_WEBHOOK: True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_active_webhook_slows_the_poll(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Once calls are pushed, polling is only a safety net."""
    with patch.object(ButterflyMXWebhookManager, "async_setup", return_value=True):
        await _setup_with_webhook(hass, config_entry)

    assert config_entry.runtime_data.calls.update_interval == timedelta(
        seconds=WEBHOOK_FALLBACK_SCAN_INTERVAL
    )


async def test_webhook_that_registers_nothing_keeps_the_fast_poll(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """If nothing is pushing, polling is still the only way calls arrive."""
    with patch.object(ButterflyMXWebhookManager, "async_setup", return_value=False):
        await _setup_with_webhook(hass, config_entry)

    assert config_entry.runtime_data.calls.update_interval == timedelta(
        seconds=DEFAULT_CALL_SCAN_INTERVAL
    )


async def test_webhook_failure_does_not_break_setup(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Push is optional, so failing to set it up must not lose the integration."""
    with patch.object(
        ButterflyMXWebhookManager, "async_setup", side_effect=RuntimeError("boom")
    ):
        await _setup_with_webhook(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(LOCK_ENTITY) is not None
    # Still polling at the normal rate, since nothing is pushing.
    assert config_entry.runtime_data.calls.update_interval == timedelta(
        seconds=DEFAULT_CALL_SCAN_INTERVAL
    )


async def test_unload(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The entry unloads cleanly."""
    await _setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
