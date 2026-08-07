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


def building_device_info(building_id: int, building_name: str | None) -> DeviceInfo:
    """Device entry representing a ButterflyMX building."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"building_{building_id}")},
        manufacturer=MANUFACTURER,
        name=building_name or f"Building {building_id}",
        model="Building",
    )


def door_device_info(target: LockTarget, building_name: str | None) -> DeviceInfo:
    """Device entry representing a single door."""
    return DeviceInfo(
        identifiers={(DOMAIN, target.device_identifier)},
        manufacturer=MANUFACTURER,
        name=target.name,
        model=target.model or "Access point",
        serial_number=target.serial_number,
        via_device=(DOMAIN, f"building_{target.building_id}"),
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
        via_device=(DOMAIN, f"building_{tenant.building_id}"),
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
    def _latest_call(self) -> Call | None:
        """Return the most recent call seen for this tenant."""
        return (self.coordinator.data or {}).get(self._tenant.id)


class ButterflyMXPassEntity(CoordinatorEntity[ButterflyMXPassCoordinator]):
    """Base entity for the visitor and delivery passes on one tenancy."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ButterflyMXPassCoordinator, tenant: Tenant
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.tenant = tenant
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
