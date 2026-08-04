"""Sensors summarising recent ButterflyMX activity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ButterflyMXConfigEntry
from .const import DOMAIN
from .coordinator import ButterflyMXCallCoordinator
from .entity import ButterflyMXCallEntity
from .models import Tenant


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a last-call sensor per tenancy."""
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
        """Initialise the sensor."""
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
