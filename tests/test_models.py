"""Tests for the ButterflyMX API models."""

from __future__ import annotations

from custom_components.butterflymx.models import (
    AccessPoint,
    ButterflyMXTopology,
    Call,
    Device,
    Tenant,
)


def test_tenant_parsing() -> None:
    """A tenant payload is parsed with its unit."""
    tenant = Tenant.from_api(
        {
            "id": 1,
            "building_id": 7,
            "building_name": "Crimson",
            "email": "ada@example.com",
            "full_name": "Ada Lovelace",
            "unit": {"id": 9, "label": "4B", "floor": "4"},
        }
    )
    assert tenant is not None
    assert tenant.display_name == "Ada Lovelace"
    assert tenant.unit_label == "4B"


def test_tenant_display_name_falls_back() -> None:
    """Missing full_name falls back to the name parts, then the email."""
    parts = Tenant.from_api(
        {"id": 1, "building_id": 7, "first_name": "Ada", "last_name": "L"}
    )
    assert parts is not None and parts.display_name == "Ada L"

    email_only = Tenant.from_api({"id": 2, "building_id": 7, "email": "a@b.c"})
    assert email_only is not None and email_only.display_name == "a@b.c"


def test_tenant_requires_ids() -> None:
    """Payloads without the identifiers we need are dropped."""
    assert Tenant.from_api({"first_name": "Ada"}) is None


def test_tenant_tolerates_null_unit() -> None:
    """``unit`` is documented as nullable."""
    tenant = Tenant.from_api({"id": 1, "building_id": 7, "unit": None})
    assert tenant is not None and tenant.unit is None


def test_access_point_parsing() -> None:
    """Device IDs are coerced to a tuple of ints."""
    point = AccessPoint.from_api(
        {"id": 100, "name": "Front", "building_id": 7, "device_ids": [500, "501", None]}
    )
    assert point is not None
    assert point.device_ids == (500, 501)


def test_access_point_default_name() -> None:
    """An unnamed access point still gets a usable name."""
    point = AccessPoint.from_api({"id": 100, "building_id": 7})
    assert point is not None and point.name == "Access point 100"


def test_device_parsing() -> None:
    """Device payloads keep the fields used for the device registry."""
    device = Device.from_api(
        {
            "id": 501,
            "name": "Apartment Door",
            "type": "smart_lock",
            "building_id": 7,
            "model": "Yale",
            "serial_number": "SN-2",
        }
    )
    assert device is not None
    assert (device.type, device.model, device.serial_number) == (
        "smart_lock",
        "Yale",
        "SN-2",
    )


def test_call_parsing_and_event_data() -> None:
    """A call payload is flattened into event data."""
    call = Call.from_api(
        {
            "id": 5,
            "building_id": 7,
            "logged_at": "2026-08-04T12:00:00Z",
            "notification_type": "visitor",
            "recipient": {"id": 1, "type": "Tenant"},
            "unit": {"id": 9},
            "device": {"id": 500, "name": "Lobby Panel"},
            "status": "initializing",
            "image_url": "https://cdn.example.com/snap.png",
        }
    )
    assert call is not None
    assert call.recipient_id == 1
    assert call.device_name == "Lobby Panel"

    data = call.as_event_data()
    assert data["call_id"] == 5
    assert data["unit_id"] == 9
    assert data["logged_at"] == "2026-08-04T12:00:00+00:00"
    # Who was called. On a multi-unit account the unit alone does not say.
    assert data["recipient_id"] == 1
    assert data["recipient_type"] == "Tenant"


def test_call_building_id_fallback() -> None:
    """A caller-supplied building ID is used when the payload omits it."""
    call = Call.from_api({"id": 5, "unit": {"id": 9}}, 7)
    assert call is not None and call.building_id == 7

    assert Call.from_api({"id": 5}) is None


def test_topology_lookups() -> None:
    """Topology helpers resolve tenants by building and unit."""
    tenant = Tenant.from_api(
        {"id": 1, "building_id": 7, "unit": {"id": 9, "label": "4B"}}
    )
    assert tenant is not None
    topology = ButterflyMXTopology(tenants=[tenant])

    assert topology.building_ids == [7]
    assert topology.tenant_for_building(7) is tenant
    assert topology.tenant_for_building(8) is None
    assert topology.tenant_for_unit(9) is tenant
    assert topology.tenant_for_unit(None) is None


def test_tenant_for_building_is_the_same_one_every_time() -> None:
    """Two tenancies in one building must not resolve by luck of the ordering.

    Whichever is picked becomes the identity a door release is performed as, so
    it has to survive a restart and an API that promises no order.
    """
    low = Tenant.from_api({"id": 3, "building_id": 7})
    high = Tenant.from_api({"id": 9, "building_id": 7})
    assert low is not None and high is not None

    assert ButterflyMXTopology(tenants=[low, high]).tenant_for_building(7) is low
    assert ButterflyMXTopology(tenants=[high, low]).tenant_for_building(7) is low


def test_building_ids_keep_their_order_and_drop_duplicates() -> None:
    """Several tenancies in one building yield that building once."""
    tenants = [
        Tenant.from_api({"id": 1, "building_id": 7}),
        Tenant.from_api({"id": 2, "building_id": 8}),
        Tenant.from_api({"id": 3, "building_id": 7}),
    ]
    topology = ButterflyMXTopology(tenants=[t for t in tenants if t])

    assert topology.building_ids == [7, 8]
