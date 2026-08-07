"""Sensors summarising recent ButterflyMX activity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ButterflyMXConfigEntry
from .const import ATTR_PASSES, DOMAIN
from .coordinator import (
    ButterflyMXAccessLogCoordinator,
    ButterflyMXCallCoordinator,
    ButterflyMXPassCoordinator,
    ButterflyMXTopologyCoordinator,
)
from .entity import (
    ButterflyMXAccessLogEntity,
    ButterflyMXCallEntity,
    ButterflyMXPassEntity,
)
from .models import Tenant
from .services import async_register_pass_services


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
            new_entities.append(
                ButterflyMXPassesSensor(runtime.passes, tenant, topology_coordinator)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(topology_coordinator.async_add_listener(_async_add_new_entities))

    # The pass services target this platform's passes sensor, so they are
    # registered alongside it.  Home Assistant only keeps one registration per
    # service name, so a second config entry setting up the same platform is
    # harmless.
    async_register_pass_services(entity_platform.async_get_current_platform())


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


class ButterflyMXPassesSensor(ButterflyMXPassEntity, SensorEntity):
    """How many visitor and delivery passes are currently valid.

    Also the target for the pass services, which is most of why it exists: an
    account can hold several tenancies, and picking this entity is what says
    which one a new pass belongs to.

    The attributes list every pass by name, window and whether it has been used,
    and deliberately carry no PIN or QR link.  Everything here is written to
    recorder and kept for weeks, which is no place for a working door code; the
    codes come back from ``butterflymx.list_passes`` instead.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "passes"
    _attr_translation_key = "active_passes"

    def __init__(
        self,
        coordinator: ButterflyMXPassCoordinator,
        tenant: Tenant,
        topology: ButterflyMXTopologyCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, tenant, topology)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_active_passes"

    @property
    def native_value(self) -> int | None:
        """Return how many passes are valid right now."""
        if self.coordinator.data is None:
            return None
        now = dt_util.utcnow()
        return sum(
            1 for record in self.passes if record.keychain.is_active(now)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List the passes on this unit, expired ones included.

        A pass that has ended is still on the account until it is revoked, so
        hiding it here would make it impossible to tidy up from an automation.
        """
        return {ATTR_PASSES: [record.as_summary() for record in self.passes]}
