"""Typed models for ButterflyMX API objects.

The API returns a fair amount of data this integration does not care about, so
each model keeps the fields it needs and stashes nothing else.  Every parser is
defensive: the sandbox and production payloads differ slightly, and fields such
as ``unit`` or ``status`` are documented as nullable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def _int_or_none(value: Any, *, field_name: str = "value") -> int | None:
    """Coerce a value to int, tolerating strings and nulls.

    A missing field is normal and stays quiet.  A field that is present but
    cannot be read is a payload the integration does not understand, so it is
    surfaced in the log rather than silently dropped.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "ButterflyMX returned %s=%r, which is not a number; ignoring it",
            field_name,
            value,
        )
        return None


def _parse_dt(value: Any, *, field_name: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 timestamp from the API.

    As with :func:`_int_or_none`, an absent value is expected but an
    unparseable one is reported.
    """
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        _LOGGER.warning(
            "ButterflyMX returned %s=%r, which is not a valid timestamp; "
            "ignoring it",
            field_name,
            value,
        )
    return parsed


def distinct_building_ids(tenants: list[Tenant]) -> list[int]:
    """Return the distinct building IDs across a list of tenants, in order."""
    seen: dict[int, None] = {}
    for tenant in tenants:
        seen.setdefault(tenant.building_id, None)
    return list(seen)


@dataclass(frozen=True, slots=True)
class Unit:
    """A unit (apartment/suite) inside a building."""

    id: int
    label: str | None = None
    floor: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Unit | None:
        """Build a Unit from an API payload."""
        unit_id = _int_or_none(data.get("id"), field_name="unit.id")
        if unit_id is None:
            _LOGGER.warning("Ignoring ButterflyMX unit with no usable id: %s", data)
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
        tenant_id = _int_or_none(data.get("id"), field_name="tenant.id")
        building_id = _int_or_none(
            data.get("building_id"), field_name="tenant.building_id"
        )
        if tenant_id is None or building_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX tenant record missing id or building_id "
                "(id=%s, building_id=%s); doors for it will not appear",
                data.get("id"),
                data.get("building_id"),
            )
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
        access_point_id = _int_or_none(data.get("id"), field_name="access_point.id")
        building_id = _int_or_none(
            data.get("building_id"), field_name="access_point.building_id"
        )
        if access_point_id is None or building_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX access point missing id or building_id "
                "(id=%s, building_id=%s); this door will not appear",
                data.get("id"),
                data.get("building_id"),
            )
            return None
        raw_device_ids = data.get("device_ids") or []
        device_ids = tuple(
            device_id
            for device_id in (
                _int_or_none(value, field_name="access_point.device_ids[]")
                for value in raw_device_ids
            )
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
        device_id = _int_or_none(data.get("id"), field_name="device.id")
        building_id = _int_or_none(
            data.get("building_id"), field_name="device.building_id"
        )
        if device_id is None or building_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX device missing id or building_id "
                "(id=%s, building_id=%s)",
                data.get("id"),
                data.get("building_id"),
            )
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
        call_id = _int_or_none(data.get("id"), field_name="call.id")
        resolved_building_id = _int_or_none(
            data.get("building_id"), field_name="call.building_id"
        )
        if resolved_building_id is None:
            resolved_building_id = building_id
        if call_id is None or resolved_building_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX call missing id or building_id "
                "(id=%s, building_id=%s); the doorbell will not fire for it",
                data.get("id"),
                data.get("building_id"),
            )
            return None

        device = data.get("device") or {}
        unit = data.get("unit") or {}
        recipient = data.get("recipient") or {}

        return cls(
            id=call_id,
            building_id=resolved_building_id,
            logged_at=_parse_dt(
                data.get("logged_at") or data.get("created_at"),
                field_name="call.logged_at",
            ),
            notification_type=data.get("notification_type"),
            status=data.get("status"),
            image_url=data.get("image_url"),
            unit_id=(
                _int_or_none(unit.get("id"), field_name="call.unit.id")
                if isinstance(unit, dict)
                else None
            ),
            device_id=(
                _int_or_none(device.get("id"), field_name="call.device.id")
                if isinstance(device, dict)
                else None
            ),
            device_name=device.get("name") if isinstance(device, dict) else None,
            recipient_id=(
                _int_or_none(recipient.get("id"), field_name="call.recipient.id")
                if isinstance(recipient, dict)
                else None
            ),
            recipient_type=(
                recipient.get("type") if isinstance(recipient, dict) else None
            ),
        )

    def as_event_data(self) -> dict[str, Any]:
        """Serialize this call for the Home Assistant event bus."""
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
            # Which resident was called.  Useful in automations on multi-unit
            # accounts, where the unit alone does not identify the person.
            "recipient_id": self.recipient_id,
            "recipient_type": self.recipient_type,
        }


@dataclass(frozen=True, slots=True)
class AccessLogEntry:
    """A door that was opened, and how."""

    id: int
    logged_at: datetime | None = None
    access_point_id: int | None = None
    release_status: str | None = None
    release_type: str | None = None
    entry_method: str | None = None
    access_tool_id: int | None = None
    image_url: str | None = None
    tenant_id: int | None = None
    unit_id: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AccessLogEntry | None:
        """Build an AccessLogEntry from an API payload."""
        entry_id = _int_or_none(data.get("id"), field_name="access_log.id")
        if entry_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX access log entry with no usable id: %s", data
            )
            return None

        # entry_method arrives either as a phrase such as "App call" or "Swipe
        # to open", or as an object naming the thing used, {"access_tool": 123}
        # for a PIN or a fob.  Both are reduced to a name, keeping the ID.
        method: Any = data.get("entry_method")
        access_tool_id: int | None = None
        if isinstance(method, dict):
            key = next(iter(method), None)
            access_tool_id = _int_or_none(
                method.get(key), field_name="access_log.entry_method"
            )
            method = key
        elif method is not None and not isinstance(method, str):
            method = str(method)

        return cls(
            id=entry_id,
            logged_at=_parse_dt(
                data.get("logged_at"), field_name="access_log.logged_at"
            ),
            access_point_id=_int_or_none(
                data.get("access_point"), field_name="access_log.access_point"
            ),
            release_status=data.get("release_status"),
            release_type=data.get("release_type"),
            entry_method=method,
            access_tool_id=access_tool_id,
            image_url=data.get("image_url"),
            tenant_id=_int_or_none(
                data.get("tenant_id"), field_name="access_log.tenant_id"
            ),
            unit_id=_int_or_none(data.get("unit"), field_name="access_log.unit"),
        )

    def as_event_data(self) -> dict[str, Any]:
        """Serialize this door release for the Home Assistant event bus."""
        return {
            "access_log_id": self.id,
            "access_point_id": self.access_point_id,
            "release_status": self.release_status,
            "release_type": self.release_type,
            "entry_method": self.entry_method,
            "access_tool_id": self.access_tool_id,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
        }


@dataclass(frozen=True, slots=True)
class VirtualKey:
    """A credential hanging off a keychain: a PIN and a QR code.

    ``pin_code`` and ``qr_code_url`` open doors.  They are what a visitor is
    given, so they have to be readable, but they must never be written anywhere
    Home Assistant keeps: not entity state, not attributes, not diagnostics.
    Recorder would hold a working door code in the database for weeks.  They
    reach the user through service response data instead, which is handed to
    the caller and then forgotten.  See :meth:`as_response`, which includes
    them, and :meth:`as_summary`, which is what everything else uses.
    """

    id: int
    keychain_id: int | None = None
    name: str | None = None
    pin_code: str | None = None
    qr_code_url: str | None = None
    instructions_url: str | None = None
    usage_count: int = 0
    first_used_at: datetime | None = None
    last_used_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> VirtualKey | None:
        """Build a VirtualKey from an API payload."""
        key_id = _int_or_none(data.get("id"), field_name="virtual_key.id")
        if key_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX virtual key with no usable id: %s",
                sorted(data),
            )
            return None
        return cls(
            id=key_id,
            keychain_id=_int_or_none(
                data.get("keychain_id"), field_name="virtual_key.keychain_id"
            ),
            name=data.get("name"),
            pin_code=data.get("pin_code"),
            qr_code_url=data.get("qr_code_url"),
            instructions_url=data.get("instructions_url"),
            usage_count=_int_or_none(
                data.get("usage_count"), field_name="virtual_key.usage_count"
            )
            or 0,
            first_used_at=_parse_dt(
                data.get("first_used_at"), field_name="virtual_key.first_used_at"
            ),
            last_used_at=_parse_dt(
                data.get("last_used_at"), field_name="virtual_key.last_used_at"
            ),
        )

    def as_summary(self) -> dict[str, Any]:
        """Describe the key without the parts that open a door."""
        return {
            "key_id": self.id,
            "name": self.name,
            "usage_count": self.usage_count,
            "first_used_at": _iso(self.first_used_at),
            "last_used_at": _iso(self.last_used_at),
        }

    def as_response(self) -> dict[str, Any]:
        """Describe the key including its credentials, for a service response."""
        return {
            **self.as_summary(),
            "pin_code": self.pin_code,
            "qr_code_url": self.qr_code_url,
            "instructions_url": self.instructions_url,
        }


@dataclass(frozen=True, slots=True)
class Keychain:
    """A grant of access: which doors, between which times.

    The keychain is the container and the thing to delete.  Its virtual keys are
    the credentials it issues, and removing it removes them too.
    """

    id: int
    name: str | None = None
    type: str | None = None
    tenant_id: int | None = None
    unit_id: int | None = None
    building_id: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    access_point_ids: tuple[int, ...] = ()
    device_ids: tuple[int, ...] = ()
    virtual_key_ids: tuple[int, ...] = ()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Keychain | None:
        """Build a Keychain from an API payload."""
        keychain_id = _int_or_none(data.get("id"), field_name="keychain.id")
        if keychain_id is None:
            _LOGGER.warning(
                "Ignoring ButterflyMX keychain with no usable id: %s", sorted(data)
            )
            return None
        return cls(
            id=keychain_id,
            name=data.get("name"),
            type=data.get("type"),
            tenant_id=_int_or_none(
                data.get("tenant_id"), field_name="keychain.tenant_id"
            ),
            unit_id=_int_or_none(data.get("unit_id"), field_name="keychain.unit_id"),
            building_id=_int_or_none(
                data.get("building_id"), field_name="keychain.building_id"
            ),
            starts_at=_parse_dt(
                data.get("starts_at"), field_name="keychain.starts_at"
            ),
            ends_at=_parse_dt(data.get("ends_at"), field_name="keychain.ends_at"),
            access_point_ids=_int_tuple(
                data.get("access_point_ids"), field_name="keychain.access_point_ids"
            ),
            device_ids=_int_tuple(
                data.get("device_ids"), field_name="keychain.device_ids"
            ),
            virtual_key_ids=_int_tuple(
                data.get("virtual_key_ids"), field_name="keychain.virtual_key_ids"
            ),
        )

    def is_active(self, now: datetime) -> bool:
        """Report whether this pass is usable at ``now``.

        A missing bound means unbounded in that direction, which is how a
        keychain with no end date behaves.
        """
        if self.starts_at is not None and now < self.starts_at:
            return False
        return self.ends_at is None or now <= self.ends_at


@dataclass(frozen=True, slots=True)
class Pass:
    """A keychain together with the codes it has issued."""

    keychain: Keychain
    keys: tuple[VirtualKey, ...] = ()

    @property
    def usage_count(self) -> int:
        """How many times this pass has been used to open a door."""
        return sum(key.usage_count for key in self.keys)

    def as_summary(self) -> dict[str, Any]:
        """Describe the pass without any credential that opens a door.

        This is what reaches entity attributes, so it has to stay free of
        ``pin_code`` and ``qr_code_url``.
        """
        keychain = self.keychain
        return {
            "pass_id": keychain.id,
            "name": keychain.name,
            "type": keychain.type,
            "starts_at": _iso(keychain.starts_at),
            "ends_at": _iso(keychain.ends_at),
            "access_point_ids": list(keychain.access_point_ids),
            "usage_count": self.usage_count,
            "used": self.usage_count > 0,
        }

    def as_response(self) -> dict[str, Any]:
        """Describe the pass including its codes, for a service response."""
        return {**self.as_summary(), "keys": [key.as_response() for key in self.keys]}


def _int_tuple(value: Any, *, field_name: str) -> tuple[int, ...]:
    """Coerce a list of IDs to a tuple of ints, dropping what will not parse."""
    if not isinstance(value, list):
        return ()
    parsed = (_int_or_none(item, field_name=f"{field_name}[]") for item in value)
    return tuple(item for item in parsed if item is not None)


def _iso(value: datetime | None) -> str | None:
    """Render a timestamp for a service response or an attribute."""
    return value.isoformat() if value else None


@dataclass(frozen=True, slots=True)
class ButterflyMXTopology:
    """Everything the integration knows about the account's doors.

    Replaced wholesale on every topology refresh rather than mutated in place,
    so it is frozen like the records it holds.
    """

    tenants: list[Tenant] = field(default_factory=list)
    access_points: list[AccessPoint] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)

    @property
    def building_ids(self) -> list[int]:
        """Distinct building IDs the user has access to."""
        return distinct_building_ids(self.tenants)

    def tenant_for_building(self, building_id: int) -> Tenant | None:
        """Return the tenant record to act as for a given building.

        An account is normally one tenancy per building.  If ButterflyMX ever
        returns several, pick the lowest ID so the choice is stable across
        restarts instead of depending on the order the API replied in.
        """
        matches = [
            tenant for tenant in self.tenants if tenant.building_id == building_id
        ]
        if not matches:
            return None
        if len(matches) > 1:
            _LOGGER.debug(
                "Building %s has %d tenant records (%s); acting as the lowest ID",
                building_id,
                len(matches),
                [tenant.id for tenant in matches],
            )
        return min(matches, key=lambda tenant: tenant.id)

    def tenant_for_unit(self, unit_id: int | None) -> Tenant | None:
        """Return the tenant record belonging to a unit."""
        if unit_id is None:
            return None
        for tenant in self.tenants:
            if tenant.unit and tenant.unit.id == unit_id:
                return tenant
        return None
