"""Doorbell event entities for ButterflyMX calls.

One event entity per tenancy.  It fires whenever a visitor calls the unit, and
carries the snapshot URL and the calling device so automations can act on it
without touching the API themselves.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ButterflyMXConfigEntry
from .const import (
    DOMAIN,
    EVENT_TYPE_DOOR_RELEASE,
    EVENT_TYPE_RING,
    PANEL_COMMAND_CALL_ENDED,
    SERVICE_DECLINE_CALL,
)
from .coordinator import ButterflyMXAccessLogCoordinator, ButterflyMXCallCoordinator
from .entity import ButterflyMXAccessLogEntity, ButterflyMXCallEntity
from .exceptions import ButterflyMXError
from .models import AccessLogEntry, Call, Tenant


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the per-tenancy event entities."""
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
            new_entities.append(ButterflyMXDoorbellEvent(runtime.calls, tenant))
            new_entities.append(
                ButterflyMXDoorReleaseEvent(runtime.access_log, tenant)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(topology_coordinator.async_add_listener(_async_add_new_entities))

    # Declining is targeted at the doorbell that is ringing, so it lives here.
    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_DECLINE_CALL, None, _async_decline_call
    )


async def _async_decline_call(entity: Entity, call: ServiceCall) -> None:
    """Stop a visitor being dialled without opening the door.

    ButterflyMX has no "decline" in v4, so this is the same command the official
    app sends from its own notification.  Without it the panel keeps ringing and
    eventually rolls the visitor over to a phone call.
    """
    if not isinstance(entity, ButterflyMXDoorbellEvent):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_doorbell",
            translation_placeholders={"entity": entity.entity_id},
        )

    live = entity.coordinator.live_call_for_tenant(entity.tenant.id)
    if live is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_call_to_decline",
            translation_placeholders={"entity": entity.entity_id},
        )

    client = entity.coordinator.client
    try:
        handle = await client.async_get_call_handle(live.id)
        if handle is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_call_to_decline",
                translation_placeholders={"entity": entity.entity_id},
            )
        await client.async_notify_panel(PANEL_COMMAND_CALL_ENDED, handle)
    except ButterflyMXError as err:
        raise HomeAssistantError(f"Could not decline the call: {err}") from err


class ButterflyMXDoorbellEvent(ButterflyMXCallEntity, EventEntity):
    """Fires when someone calls the unit from an intercom."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_translation_key = "doorbell"
    _attr_event_types: ClassVar[list[str]] = [EVENT_TYPE_RING]

    def __init__(
        self, coordinator: ButterflyMXCallCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the doorbell event entity."""
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
        self._trigger_event(EVENT_TYPE_RING, call.as_event_data())
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on a refresh without treating it as a new event.

        The doorbell itself is fired by the call listener, not by polling, so
        the base implementation's behavior is not wanted here.  State still has
        to be written, or a coordinator that starts failing would leave this
        entity looking available while every other entity goes unavailable.
        """
        self.async_write_ha_state()


class ButterflyMXDoorReleaseEvent(ButterflyMXAccessLogEntity, EventEntity):
    """Fires when a door on this tenancy is opened.

    Covers every way a door is opened, not just this integration: a PIN at the
    keypad, a fob, answering the intercom in the app, or a release sent from
    here.
    """

    _attr_translation_key = "door_release"
    _attr_event_types: ClassVar[list[str]] = [EVENT_TYPE_DOOR_RELEASE]

    def __init__(
        self, coordinator: ButterflyMXAccessLogCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the door release event entity."""
        super().__init__(coordinator, tenant)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_door_release"

    async def async_added_to_hass(self) -> None:
        """Subscribe to door releases."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_release_listener(self._handle_release)
        )

    @callback
    def _handle_release(self, tenant: Tenant, entry: AccessLogEntry) -> None:
        """Fire for releases belonging to this tenancy."""
        if tenant.id != self._tenant.id:
            return
        self._trigger_event(EVENT_TYPE_DOOR_RELEASE, entry.as_event_data())
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on a refresh without treating it as a new event."""
        self.async_write_ha_state()
