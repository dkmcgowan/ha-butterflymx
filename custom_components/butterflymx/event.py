"""Doorbell event entities for ButterflyMX calls.

One event entity per tenancy.  It fires whenever a visitor calls the unit, and
carries the snapshot URL and the calling device so automations can act on it
without touching the API themselves.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ButterflyMXConfigEntry
from .const import DOMAIN, EVENT_TYPE_CALL
from .coordinator import ButterflyMXCallCoordinator
from .entity import ButterflyMXCallEntity
from .models import Call, Tenant


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a doorbell event entity per tenancy."""
    runtime = entry.runtime_data
    topology_coordinator = runtime.topology

    @callback
    def _async_add_new_entities() -> None:
        topology = topology_coordinator.data
        if topology is None:
            return
        new_entities = [
            ButterflyMXDoorbellEvent(runtime.calls, tenant)
            for tenant in topology.tenants
            if tenant.id not in runtime.known_tenant_ids
        ]
        for tenant in topology.tenants:
            runtime.known_tenant_ids.add(tenant.id)
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(topology_coordinator.async_add_listener(_async_add_new_entities))


class ButterflyMXDoorbellEvent(ButterflyMXCallEntity, EventEntity):
    """Fires when someone calls the unit from an intercom."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_translation_key = "doorbell"
    _attr_event_types: ClassVar[list[str]] = [EVENT_TYPE_CALL]

    def __init__(
        self, coordinator: ButterflyMXCallCoordinator, tenant: Tenant
    ) -> None:
        """Initialise the doorbell event entity."""
        super().__init__(coordinator, tenant)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_doorbell"

    async def async_added_to_hass(self) -> None:
        """Subscribe to new calls."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_call_listener(self._handle_call)
        )

    @callback
    def _handle_call(self, tenant: Tenant, call: Call) -> None:
        """Fire the doorbell for calls addressed to this tenancy."""
        if tenant.id != self._tenant.id:
            return
        self._trigger_event(EVENT_TYPE_CALL, call.as_event_data())
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Ignore coordinator refreshes; events come from the listener."""
