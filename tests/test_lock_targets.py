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
    assert targets[0].unique_key == "tenant_1_access_point_100"
    assert targets[0].access_point_id == 100
    assert targets[0].device_id is None
    assert targets[0].tenant_id == 1


def test_locks_not_behind_an_access_point_are_addressed_by_device() -> None:
    """Doors belong to buildings, never to units.

    Most arrive as access points. One that does not has to be released by
    device ID instead.
    """
    topology = ButterflyMXTopology(
        tenants=[_tenant()], devices=[_device(501, 7, "smart_lock")]
    )
    targets = build_lock_targets(topology)

    assert len(targets) == 1
    assert targets[0].unique_key == "tenant_1_device_501"
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

    assert [target.unique_key for target in targets] == ["tenant_1_access_point_100"]


def test_two_residents_of_one_building_get_their_own_locks() -> None:
    """The same door, set up twice, must not produce the same identity.

    Two people in a house who each have a ButterflyMX login set the integration
    up twice, and Home Assistant gives each config entry its own entities and
    devices. If the identity were the door alone, the second setup's locks
    would collide and be dropped.

    It is not only a registry problem. A release is performed as a tenant and
    the access log records who, so these really are two different locks that
    happen to open one door.
    """
    door = _access_point(100, 7, [])
    mine = build_lock_targets(
        ButterflyMXTopology(tenants=[_tenant(tenant_id=1)], access_points=[door])
    )
    theirs = build_lock_targets(
        ButterflyMXTopology(tenants=[_tenant(tenant_id=2)], access_points=[door])
    )

    assert mine[0].unique_key != theirs[0].unique_key
    assert mine[0].device_identifier != theirs[0].device_identifier
    # Same door, opened as different people.
    assert mine[0].access_point_id == theirs[0].access_point_id == 100
    assert (mine[0].tenant_id, theirs[0].tenant_id) == (1, 2)


def test_doors_without_a_tenant_are_skipped() -> None:
    """We can only open doors as a tenant of that building."""
    topology = ButterflyMXTopology(
        tenants=[_tenant(building_id=7)],
        access_points=[_access_point(100, 8, [])],
        devices=[_device(501, 8, "smart_lock")],
    )
    assert build_lock_targets(topology) == []
