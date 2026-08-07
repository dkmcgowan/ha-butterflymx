"""Sensors summarising recent ButterflyMX activity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ButterflyMXConfigEntry
from .const import DOMAIN
from .coordinator import ButterflyMXAccessLogCoordinator, ButterflyMXCallCoordinator
from .entity import ButterflyMXAccessLogEntity, ButterflyMXCallEntity
from .models import Tenant


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the per-tenancy sensors."""
    runtime = entry.runtime_data
    topology_coordinator = runtime.topology
    created: set[int] = set()

    @callback
    def _async_add_new_entities() -> None:
        topology = topology_coordinator.data
        if topology is None:
            return
        new_entities = []
        for tenant in topology.tenants:
            if tenant.id in created:
                continue
            created.add(tenant.id)
            new_entities.append(ButterflyMXLastCallSensor(runtime.calls, tenant))
            new_entities.append(
                ButterflyMXLastDoorReleaseSensor(runtime.access_log, tenant)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(topology_coordinator.async_add_listener(_async_add_new_entities))


class ButterflyMXLastCallSensor(ButterflyMXCallEntity, SensorEntity):
    """Timestamp of the most recent call to this unit."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_call"

    def __init__(
        self, coordinator: ButterflyMXCallCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, tenant)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_last_call"

    @property
    def native_value(self) -> datetime | None:
        """Return when the last call happened."""
        call = self._latest_call
        return call.logged_at if call else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose details of the last call."""
        call = self._latest_call
        if call is None:
            return {}
        return {
            "call_id": call.id,
            "notification_type": call.notification_type,
            "status": call.status,
            "device_name": call.device_name,
            "image_url": call.image_url,
        }


class ButterflyMXLastDoorReleaseSensor(ButterflyMXAccessLogEntity, SensorEntity):
    """Timestamp of the last door opened on this tenancy."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_door_release"

    def __init__(
        self, coordinator: ButterflyMXAccessLogCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, tenant)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_last_door_release"

    @property
    def native_value(self) -> datetime | None:
        """Return when a door was last opened."""
        entry = self._latest_release
        return entry.logged_at if entry else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which door was opened and how."""
        entry = self._latest_release
        if entry is None:
            return {}
        return {
            "access_point_id": entry.access_point_id,
            "release_status": entry.release_status,
            "release_type": entry.release_type,
            "entry_method": entry.entry_method,
            "access_tool_id": entry.access_tool_id,
        }
