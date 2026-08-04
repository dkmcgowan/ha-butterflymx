"""Tests for turning the account topology into lock entities."""

from __future__ import annotations

from custom_components.butterflymx.coordinator import build_lock_targets
from custom_components.butterflymx.models import (
    AccessPoint,
    ButterflyMXTopology,
    Device,
    Tenant,
)


def _tenant(tenant_id: int = 1, building_id: int = 7) -> Tenant:
    tenant = Tenant.from_api({"id": tenant_id, "building_id": building_id})
    assert tenant is not None
    return tenant


def _access_point(point_id: int, building_id: int, device_ids: list[int]) -> AccessPoint:
    point = AccessPoint.from_api(
        {
            "id": point_id,
            "name": f"Door {point_id}",
            "building_id": building_id,
            "device_ids": device_ids,
        }
    )
    assert point is not None
    return point


def _device(device_id: int, building_id: int, device_type: str) -> Device:
    device = Device.from_api(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "building_id": building_id,
            "type": device_type,
        }
    )
    assert device is not None
    return device


def test_access_points_become_locks() -> None:
    """Every access point in a building the user is a tenant of becomes a lock."""
    topology = ButterflyMXTopology(
        tenants=[_tenant()], access_points=[_access_point(100, 7, [500])]
    )
    targets = build_lock_targets(topology)

    assert len(targets) == 1
    assert targets[0].unique_key == "access_point_100"
    assert targets[0].access_point_id == 100
    assert targets[0].device_id is None
    assert targets[0].tenant_id == 1


def test_unit_smart_locks_become_locks() -> None:
    """Unit smart locks are addressed by device ID."""
    topology = ButterflyMXTopology(
        tenants=[_tenant()], devices=[_device(501, 7, "smart_lock")]
    )
    targets = build_lock_targets(topology)

    assert len(targets) == 1
    assert targets[0].unique_key == "device_501"
    assert targets[0].device_id == 501
    assert targets[0].access_point_id is None


def test_non_lock_devices_are_ignored() -> None:
    """Panels, keypads and controllers are not doors on their own."""
    topology = ButterflyMXTopology(
        tenants=[_tenant()],
        devices=[
            _device(500, 7, "panel"),
            _device(502, 7, "cloud_based_access_controller"),
        ],
    )
    assert build_lock_targets(topology) == []


def test_devices_behind_an_access_point_are_not_duplicated() -> None:
    """A smart lock reachable through an access point yields one entity."""
    topology = ButterflyMXTopology(
        tenants=[_tenant()],
        access_points=[_access_point(100, 7, [501])],
        devices=[_device(501, 7, "smart_lock")],
    )
    targets = build_lock_targets(topology)

    assert [target.unique_key for target in targets] == ["access_point_100"]


def test_doors_without_a_tenant_are_skipped() -> None:
    """We can only open doors as a tenant of that building."""
    topology = ButterflyMXTopology(
        tenants=[_tenant(building_id=7)],
        access_points=[_access_point(100, 8, [])],
        devices=[_device(501, 8, "smart_lock")],
    )
    assert build_lock_targets(topology) == []
