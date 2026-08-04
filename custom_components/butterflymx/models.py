"""Typed models for ButterflyMX API objects.

The API returns a fair amount of data this integration does not care about, so
each model keeps the fields it needs and stashes nothing else.  Every parser is
defensive: the sandbox and production payloads differ slightly, and fields such
as ``unit`` or ``status`` are documented as nullable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def _int_or_none(value: Any) -> int | None:
    """Coerce a value to int, tolerating strings and nulls."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API."""
    if not value:
        return None
    return dt_util.parse_datetime(str(value))


@dataclass(frozen=True, slots=True)
class Unit:
    """A unit (apartment/suite) inside a building."""

    id: int
    label: str | None = None
    floor: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Unit | None:
        """Build a Unit from an API payload."""
        unit_id = _int_or_none(data.get("id"))
        if unit_id is None:
            return None
        return cls(
            id=unit_id,
            label=data.get("label"),
            floor=data.get("floor"),
        )


@dataclass(frozen=True, slots=True)
class Tenant:
    """A tenant record, the identity a door release is performed as."""

    id: int
    building_id: int
    building_name: str | None = None
    building_timezone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    unit: Unit | None = None

    @property
    def display_name(self) -> str:
        """Human readable name for this tenant."""
        if self.full_name:
            return self.full_name
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return " ".join(parts)
        return self.email or f"Tenant {self.id}"

    @property
    def unit_label(self) -> str | None:
        """Label of the unit this tenant belongs to."""
        return self.unit.label if self.unit else None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Tenant | None:
        """Build a Tenant from an API payload."""
        tenant_id = _int_or_none(data.get("id"))
        building_id = _int_or_none(data.get("building_id"))
        if tenant_id is None or building_id is None:
            return None
        unit_data = data.get("unit")
        return cls(
            id=tenant_id,
            building_id=building_id,
            building_name=data.get("building_name"),
            building_timezone=data.get("building_timezone"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            full_name=data.get("full_name"),
            email=data.get("email"),
            unit=Unit.from_api(unit_data) if isinstance(unit_data, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """An access point: intercom, ACS controller, keypad or common-area lock."""

    id: int
    building_id: int
    name: str
    device_ids: tuple[int, ...] = ()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AccessPoint | None:
        """Build an AccessPoint from an API payload."""
        access_point_id = _int_or_none(data.get("id"))
        building_id = _int_or_none(data.get("building_id"))
        if access_point_id is None or building_id is None:
            return None
        raw_device_ids = data.get("device_ids") or []
        device_ids = tuple(
            device_id
            for device_id in (_int_or_none(value) for value in raw_device_ids)
            if device_id is not None
        )
        return cls(
            id=access_point_id,
            building_id=building_id,
            name=data.get("name") or f"Access point {access_point_id}",
            device_ids=device_ids,
        )


@dataclass(frozen=True, slots=True)
class Device:
    """A hardware device installed in a building."""

    id: int
    building_id: int
    name: str
    type: str | None = None
    model: str | None = None
    serial_number: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device | None:
        """Build a Device from an API payload."""
        device_id = _int_or_none(data.get("id"))
        building_id = _int_or_none(data.get("building_id"))
        if device_id is None or building_id is None:
            return None
        return cls(
            id=device_id,
            building_id=building_id,
            name=data.get("name") or f"Device {device_id}",
            type=data.get("type"),
            model=data.get("model"),
            serial_number=data.get("serial_number"),
        )


@dataclass(frozen=True, slots=True)
class Call:
    """A call placed from a building device to a tenant."""

    id: int
    building_id: int
    logged_at: datetime | None = None
    notification_type: str | None = None
    status: str | None = None
    image_url: str | None = None
    unit_id: int | None = None
    device_id: int | None = None
    device_name: str | None = None
    recipient_id: int | None = None
    recipient_type: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any], building_id: int | None = None) -> Call | None:
        """Build a Call from an API payload."""
        call_id = _int_or_none(data.get("id"))
        resolved_building_id = _int_or_none(data.get("building_id"))
        if resolved_building_id is None:
            resolved_building_id = building_id
        if call_id is None or resolved_building_id is None:
            return None

        device = data.get("device") or {}
        unit = data.get("unit") or {}
        recipient = data.get("recipient") or {}

        return cls(
            id=call_id,
            building_id=resolved_building_id,
            logged_at=_parse_dt(data.get("logged_at") or data.get("created_at")),
            notification_type=data.get("notification_type"),
            status=data.get("status"),
            image_url=data.get("image_url"),
            unit_id=_int_or_none(unit.get("id")) if isinstance(unit, dict) else None,
            device_id=_int_or_none(device.get("id")) if isinstance(device, dict) else None,
            device_name=device.get("name") if isinstance(device, dict) else None,
            recipient_id=(
                _int_or_none(recipient.get("id")) if isinstance(recipient, dict) else None
            ),
            recipient_type=(
                recipient.get("type") if isinstance(recipient, dict) else None
            ),
        )

    def as_event_data(self) -> dict[str, Any]:
        """Serialise this call for the Home Assistant event bus."""
        return {
            "call_id": self.id,
            "building_id": self.building_id,
            "unit_id": self.unit_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "notification_type": self.notification_type,
            "status": self.status,
            "image_url": self.image_url,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
        }


@dataclass(slots=True)
class ButterflyMXTopology:
    """Everything the integration knows about the account's doors."""

    tenants: list[Tenant] = field(default_factory=list)
    access_points: list[AccessPoint] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)

    @property
    def building_ids(self) -> list[int]:
        """Distinct building IDs the user has access to."""
        seen: dict[int, None] = {}
        for tenant in self.tenants:
            seen.setdefault(tenant.building_id, None)
        return list(seen)

    def tenant_for_building(self, building_id: int) -> Tenant | None:
        """Return the tenant record to act as for a given building."""
        for tenant in self.tenants:
            if tenant.building_id == building_id:
                return tenant
        return None

    def tenant_for_unit(self, unit_id: int | None) -> Tenant | None:
        """Return the tenant record belonging to a unit."""
        if unit_id is None:
            return None
        for tenant in self.tenants:
            if tenant.unit and tenant.unit.id == unit_id:
                return tenant
        return None
