"""Data coordinators for the ButterflyMX integration.

Two loops with very different cadences:

* :class:`ButterflyMXTopologyCoordinator` refreshes the account's tenants,
  access points and devices once an hour, because doors do not move around.
* :class:`ButterflyMXCallCoordinator` polls the call log every few seconds so a
  visitor buzzing the intercom shows up quickly.  When webhook push is enabled
  the same coordinator accepts calls handed to it by the webhook view.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ButterflyMXClient
from .const import (
    CALL_LOOKBACK,
    DOMAIN,
    EVENT_CALL,
    TOPOLOGY_SCAN_INTERVAL,
    UNIT_LOCK_DEVICE_TYPES,
)
from .exceptions import (
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
    ButterflyMXError,
)
from .models import AccessPoint, ButterflyMXTopology, Call, Device, Tenant

if TYPE_CHECKING:
    from . import ButterflyMXConfigEntry

_LOGGER = logging.getLogger(__name__)

# Remember this many call IDs per building so a webhook and a poll delivering
# the same call do not fire the doorbell twice.
_SEEN_CALL_LIMIT = 200


@dataclass(frozen=True, slots=True)
class LockTarget:
    """A door this integration can open."""

    unique_key: str
    name: str
    tenant_id: int
    building_id: int
    access_point_id: int | None = None
    device_id: int | None = None
    model: str | None = None
    serial_number: str | None = None

    @property
    def device_identifier(self) -> str:
        """Stable identifier used in the device registry."""
        return self.unique_key


def build_lock_targets(topology: ButterflyMXTopology) -> list[LockTarget]:
    """Work out which doors should become lock entities.

    Access points cover intercoms, ACS controllers, keypads and common-area
    locks.  Unit-level smart locks are not access points and have to be released
    by ``device_id`` instead, so they are added separately, skipping any device
    already reachable through an access point to avoid duplicate entities.
    """
    targets: list[LockTarget] = []
    claimed_device_ids: set[int] = set()

    for access_point in topology.access_points:
        tenant = topology.tenant_for_building(access_point.building_id)
        if tenant is None:
            _LOGGER.debug(
                "Skipping access point %s: no tenant record for building %s",
                access_point.id,
                access_point.building_id,
            )
            continue
        claimed_device_ids.update(access_point.device_ids)
        targets.append(
            LockTarget(
                unique_key=f"access_point_{access_point.id}",
                name=access_point.name,
                tenant_id=tenant.id,
                building_id=access_point.building_id,
                access_point_id=access_point.id,
            )
        )

    for device in topology.devices:
        if device.id in claimed_device_ids:
            continue
        if (device.type or "").lower() not in UNIT_LOCK_DEVICE_TYPES:
            continue
        tenant = topology.tenant_for_building(device.building_id)
        if tenant is None:
            continue
        targets.append(
            LockTarget(
                unique_key=f"device_{device.id}",
                name=device.name,
                tenant_id=tenant.id,
                building_id=device.building_id,
                device_id=device.id,
                model=device.model or device.type,
                serial_number=device.serial_number,
            )
        )

    return targets


class ButterflyMXTopologyCoordinator(DataUpdateCoordinator[ButterflyMXTopology]):
    """Keeps the list of tenants, access points and devices up to date."""

    config_entry: ButterflyMXConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ButterflyMXClient,
    ) -> None:
        """Initialise the topology coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} topology",
            update_interval=timedelta(seconds=TOPOLOGY_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> ButterflyMXTopology:
        """Fetch the account topology."""
        try:
            tenants = await self.client.async_get_tenants()
            if not tenants:
                raise UpdateFailed(
                    "ButterflyMX returned no tenant records for this account"
                )

            access_points: list[AccessPoint] = []
            devices: list[Device] = []
            for building_id in _distinct_building_ids(tenants):
                access_points.extend(
                    await self.client.async_get_access_points(building_id)
                )
                try:
                    devices.extend(await self.client.async_get_devices(building_id))
                except ButterflyMXAuthError:
                    raise
                except ButterflyMXError as err:
                    # Residents may not be allowed to enumerate building devices.
                    _LOGGER.debug(
                        "Could not list devices for building %s: %s", building_id, err
                    )
        except ButterflyMXAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ButterflyMXConnectionError as err:
            raise UpdateFailed(f"Could not reach ButterflyMX: {err}") from err
        except ButterflyMXError as err:
            raise UpdateFailed(str(err)) from err

        return ButterflyMXTopology(
            tenants=tenants, access_points=access_points, devices=devices
        )


def _distinct_building_ids(tenants: list[Tenant]) -> list[int]:
    """Return the distinct building IDs across a list of tenants."""
    seen: dict[int, None] = {}
    for tenant in tenants:
        seen.setdefault(tenant.building_id, None)
    return list(seen)


class ButterflyMXCallCoordinator(DataUpdateCoordinator[dict[int, Call]]):
    """Polls the call log and announces new calls.

    ``data`` maps a tenant ID to the most recent call for that tenant.
    """

    config_entry: ButterflyMXConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ButterflyMXClient,
        topology_coordinator: ButterflyMXTopologyCoordinator,
        scan_interval: int,
    ) -> None:
        """Initialise the call coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} calls",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.topology_coordinator = topology_coordinator
        self._seen_call_ids: dict[int, None] = {}
        self._since: datetime | None = None
        self._call_listeners: list[Callable[[Tenant, Call], None]] = []
        self._push_lock = asyncio.Lock()
        # The first poll happens during setup and would otherwise re-announce
        # every call that came in while Home Assistant was down.
        self._priming = True

    @callback
    def async_add_call_listener(
        self, listener: Callable[[Tenant, Call], None]
    ) -> Callable[[], None]:
        """Register a callback fired for each newly observed call."""
        self._call_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._call_listeners:
                self._call_listeners.remove(listener)

        return _remove

    async def _async_update_data(self) -> dict[int, Call]:
        """Poll every building the user has a tenancy in."""
        topology = self.topology_coordinator.data
        if topology is None:
            return dict(self.data or {})

        since = self._since or dt_util.utcnow() - timedelta(seconds=CALL_LOOKBACK)
        # Overlap slightly so a call logged during the previous request is not
        # missed because of clock skew between us and the API.
        poll_started = dt_util.utcnow()

        calls: list[Call] = []
        try:
            for building_id in topology.building_ids:
                calls.extend(
                    await self.client.async_get_calls(building_id, since=since)
                )
        except ButterflyMXAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ButterflyMXConnectionError as err:
            raise UpdateFailed(f"Could not reach ButterflyMX: {err}") from err
        except ButterflyMXError as err:
            raise UpdateFailed(str(err)) from err

        self._since = poll_started - timedelta(seconds=30)
        data = self._process_calls(calls)
        self._priming = False
        return data

    async def async_handle_pushed_call(self, call: Call) -> None:
        """Feed a call delivered by webhook into the same pipeline."""
        async with self._push_lock:
            data = self._process_calls([call])
            self.async_set_updated_data(data)

    def _process_calls(self, calls: list[Call]) -> dict[int, Call]:
        """Deduplicate, announce and index new calls by tenant."""
        topology = self.topology_coordinator.data
        latest: dict[int, Call] = dict(self.data or {})

        fresh = [call for call in calls if call.id not in self._seen_call_ids]
        fresh.sort(key=lambda call: (call.logged_at or dt_util.utc_from_timestamp(0), call.id))

        for call in fresh:
            self._remember(call.id)
            tenant = self._match_tenant(call, topology)
            if tenant is None:
                _LOGGER.debug("Ignoring call %s: no matching tenant", call.id)
                continue

            previous = latest.get(tenant.id)
            if previous is not None and _is_older(call, previous):
                continue
            latest[tenant.id] = call

            if self._priming:
                # Seed the "last call" entities without ringing the doorbell.
                continue

            self.hass.bus.async_fire(
                EVENT_CALL,
                {
                    "entry_id": self.config_entry.entry_id,
                    "tenant_id": tenant.id,
                    "unit_label": tenant.unit_label,
                    **call.as_event_data(),
                },
            )
            for listener in list(self._call_listeners):
                listener(tenant, call)

        return latest

    def _remember(self, call_id: int) -> None:
        """Record a call ID, evicting the oldest once the cache is full."""
        self._seen_call_ids[call_id] = None
        while len(self._seen_call_ids) > _SEEN_CALL_LIMIT:
            self._seen_call_ids.pop(next(iter(self._seen_call_ids)))

    @staticmethod
    def _match_tenant(call: Call, topology: ButterflyMXTopology | None) -> Tenant | None:
        """Find the tenant a call was placed to."""
        if topology is None:
            return None
        if call.recipient_type == "Tenant" and call.recipient_id is not None:
            for tenant in topology.tenants:
                if tenant.id == call.recipient_id:
                    return tenant
        matched = topology.tenant_for_unit(call.unit_id)
        if matched is not None:
            return matched
        # Single-tenancy accounts: a building match is unambiguous.
        building_tenants = [
            tenant for tenant in topology.tenants if tenant.building_id == call.building_id
        ]
        if len(building_tenants) == 1:
            return building_tenants[0]
        return None


def _is_older(call: Call, other: Call) -> bool:
    """Return True when ``call`` happened before ``other``."""
    if call.logged_at and other.logged_at:
        return call.logged_at < other.logged_at
    return call.id < other.id
