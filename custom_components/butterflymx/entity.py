"""Shared entity plumbing for the ButterflyMX integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ButterflyMXClient
from .const import DOMAIN, MANUFACTURER
from .coordinator import (
    ButterflyMXAccessLogCoordinator,
    ButterflyMXCallCoordinator,
    ButterflyMXPassCoordinator,
    ButterflyMXTopologyCoordinator,
    LockTarget,
)
from .models import AccessLogEntry, Call, Pass, Tenant


def building_identifier(tenant_id: int, building_id: int) -> str:
    """Registry identifier for a building, as seen by one tenancy.

    Every identifier this integration creates starts with the tenancy, because
    a config entry is one ButterflyMX login and Home Assistant gives each entry
    its own devices -- ``DeviceEntry.config_entry_id`` is a single entry, and
    removing an entry removes its devices outright.  Two residents of the same
    building who each set the integration up therefore need two sets, not one
    set fought over by both.
    """
    return f"tenant_{tenant_id}_building_{building_id}"


def building_device_info(tenant: Tenant) -> DeviceInfo:
    """Device entry representing a ButterflyMX building.

    Registered explicitly during setup rather than implicitly by an entity,
    because nothing else has a reason to live on it and the doors and the unit
    both point at it as their parent.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, building_identifier(tenant.id, tenant.building_id))},
        manufacturer=MANUFACTURER,
        name=tenant.building_name or f"Building {tenant.building_id}",
        model="Building",
    )


def door_device_info(target: LockTarget) -> DeviceInfo:
    """Device entry representing a single door, as one tenancy."""
    return DeviceInfo(
        identifiers={(DOMAIN, target.device_identifier)},
        manufacturer=MANUFACTURER,
        name=target.name,
        model=target.model or "Access point",
        serial_number=target.serial_number,
        via_device=(DOMAIN, building_identifier(target.tenant_id, target.building_id)),
    )


def unit_device_info(tenant: Tenant) -> DeviceInfo:
    """Device entry representing the user's unit / intercom station."""
    label = tenant.unit_label
    name = f"Unit {label}" if label else tenant.display_name
    return DeviceInfo(
        identifiers={(DOMAIN, f"tenant_{tenant.id}")},
        manufacturer=MANUFACTURER,
        name=name,
        model="Intercom",
        via_device=(DOMAIN, building_identifier(tenant.id, tenant.building_id)),
    )


class ButterflyMXTopologyEntity(CoordinatorEntity[ButterflyMXTopologyCoordinator]):
    """Base entity for things derived from the account topology."""

    _attr_has_entity_name = True


class ButterflyMXCallEntity(CoordinatorEntity[ButterflyMXCallCoordinator]):
    """Base entity for things derived from the call log."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ButterflyMXCallCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._tenant = tenant
        self._attr_device_info = unit_device_info(tenant)

    @property
    def tenant(self) -> Tenant:
        """Return the tenancy this entity belongs to."""
        return self._tenant

    @property
    def _latest_call(self) -> Call | None:
        """Return the most recent call seen for this tenant."""
        return (self.coordinator.data or {}).get(self._tenant.id)


class ButterflyMXPassEntity(CoordinatorEntity[ButterflyMXPassCoordinator]):
    """Base entity for the visitor and delivery passes on one tenancy."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ButterflyMXPassCoordinator,
        tenant: Tenant,
        topology: ButterflyMXTopologyCoordinator,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.tenant = tenant
        # Carried so a pass can be scoped to particular doors: the service is
        # given lock entities and has to find the doors behind them.
        self.topology = topology
        self._attr_device_info = unit_device_info(tenant)

    @property
    def client(self) -> ButterflyMXClient:
        """Return the API client, for the services that act on passes."""
        return self.coordinator.client

    @property
    def passes(self) -> list[Pass]:
        """Return this tenancy's passes, soonest to expire first."""
        return self.coordinator.passes_for_tenant(self.tenant)


class ButterflyMXAccessLogEntity(CoordinatorEntity[ButterflyMXAccessLogCoordinator]):
    """Base entity for things derived from the door release log."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ButterflyMXAccessLogCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._tenant = tenant
        self._attr_device_info = unit_device_info(tenant)

    @property
    def _latest_release(self) -> AccessLogEntry | None:
        """Return the most recent door release seen for this tenancy."""
        return (self.coordinator.data or {}).get(self._tenant.id)
