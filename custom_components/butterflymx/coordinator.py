"""Data coordinators for the ButterflyMX integration.

Two loops with very different cadences:

* :class:`ButterflyMXTopologyCoordinator` refreshes the account's tenants,
  access points and devices once an hour, because doors do not move around.
* :class:`ButterflyMXCallCoordinator` polls the call log every few seconds so a
  visitor buzzing the intercom shows up quickly.  Webhook push, where it is
  available, does not feed calls in from the side: it asks this coordinator to
  read the log immediately.  A delivery carries no usable call ID, so the log
  stays the only place a call is ever read from and deduplication keeps working
  on one set of IDs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ButterflyMXClient
from .const import (
    ACCESS_LOG_LOOKBACK,
    ACCESS_LOG_SCAN_INTERVAL,
    CALL_LOOKBACK,
    CALL_POLL_OVERLAP,
    DIRECT_LOCK_DEVICE_TYPES,
    DOMAIN,
    EVENT_CALL,
    EVENT_DOOR_RELEASE,
    PASS_SCAN_INTERVAL,
    TOPOLOGY_SCAN_INTERVAL,
)
from .exceptions import (
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
    ButterflyMXError,
)
from .models import (
    AccessLogEntry,
    AccessPoint,
    AccessTool,
    ButterflyMXTopology,
    Call,
    Device,
    Pass,
    Tenant,
    VirtualKey,
    distinct_building_ids,
)

if TYPE_CHECKING:
    from . import ButterflyMXConfigEntry

_LOGGER = logging.getLogger(__name__)

# Remember this many call IDs per building so a webhook and a poll delivering
# the same call do not fire the doorbell twice.
_SEEN_CALL_LIMIT = 200

# Sorts a pass with no end date last, rather than crashing on a None comparison.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LockTarget:
    """A door this integration can open, as one tenancy.

    Note the "as one tenancy" part.  A door is shared, but opening it is not: a
    release is performed as a particular tenant and the access log records who.
    So a household where two residents each have their own ButterflyMX login
    and set the integration up twice has two genuinely different locks on one
    physical door, and each one's releases are attributed to its own resident.
    That is why the tenant is part of the identity here rather than an
    incidental field.
    """

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

    Every door belongs to a building.  The tenancy is only the identity the
    release is performed as, which is why each target is resolved through
    :meth:`ButterflyMXTopology.tenant_for_building` rather than through a unit.

    Access points cover intercoms, ACS controllers, keypads and common-area
    locks.  A few doors are not access points and have to be released by
    ``device_id`` instead, so they are added separately, skipping any device
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
                unique_key=f"tenant_{tenant.id}_access_point_{access_point.id}",
                name=access_point.name,
                tenant_id=tenant.id,
                building_id=access_point.building_id,
                access_point_id=access_point.id,
            )
        )

    for device in topology.devices:
        if device.id in claimed_device_ids:
            continue
        if (device.type or "").lower() not in DIRECT_LOCK_DEVICE_TYPES:
            continue
        tenant = topology.tenant_for_building(device.building_id)
        if tenant is None:
            continue
        targets.append(
            LockTarget(
                unique_key=f"tenant_{tenant.id}_device_{device.id}",
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
        """Initialize the topology coordinator."""
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
            building_ids = distinct_building_ids(tenants)
            unreachable: list[int] = []

            # Buildings are independent.  One of them failing must not take the
            # others down with it: an account can span several buildings, and a
            # problem with one is no reason to lose the doors in the rest.
            for building_id in building_ids:
                try:
                    access_points.extend(
                        await self.client.async_get_access_points(building_id)
                    )
                except ButterflyMXAuthError:
                    raise
                except ButterflyMXError as err:
                    unreachable.append(building_id)
                    _LOGGER.warning(
                        "Could not list access points for building %s: %s. Its "
                        "doors are unavailable until the next refresh; other "
                        "buildings are unaffected",
                        building_id,
                        err,
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

            # Names for the PINs and fobs the access log refers to.  Not worth
            # failing the refresh over: without them a door opening still
            # reports, it just says "access_tool 8432576" instead of "PIN".
            access_tools: list[AccessTool] = []
            try:
                access_tools = await self.client.async_get_access_tools()
            except ButterflyMXAuthError:
                raise
            except ButterflyMXError as err:
                _LOGGER.debug("Could not list access tools: %s", err)

            if unreachable and len(unreachable) == len(building_ids):
                # Nothing came back at all, so this is a general failure rather
                # than one bad building.  Fail the refresh and keep the previous
                # topology instead of reporting that every door has gone away.
                raise UpdateFailed(
                    f"Could not list access points for any building "
                    f"({', '.join(str(b) for b in unreachable)})"
                )
        except ButterflyMXAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ButterflyMXConnectionError as err:
            raise UpdateFailed(f"Could not reach ButterflyMX: {err}") from err
        except ButterflyMXError as err:
            raise UpdateFailed(str(err)) from err

        return ButterflyMXTopology(
            tenants=tenants,
            access_points=access_points,
            devices=devices,
            access_tools=access_tools,
        )


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
        """Initialize the call coordinator."""
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
        # The first poll happens during setup and would otherwise re-announce
        # every call that came in while Home Assistant was down.
        #
        # Anything processed while this is set is marked seen without ringing
        # the doorbell, so a real call arriving now would be lost.  Setup
        # avoids that by priming before the webhook is registered; see the
        # ordering note in async_setup_entry.
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

        # Start the next window slightly before this one ended; see
        # CALL_POLL_OVERLAP for why.
        self._since = poll_started - timedelta(seconds=CALL_POLL_OVERLAP)
        data = self._process_calls(calls)
        self._priming = False
        return data

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
                # Should not happen: the API only returns calls for buildings
                # this account has a tenancy in, so one of the three matches in
                # _match_tenant ought to land.  If it does not, somebody rang a
                # doorbell and nothing happened, which is worth saying out loud.
                _LOGGER.warning(
                    "Ignoring ButterflyMX call %s in building %s: it does not "
                    "match any known tenant (recipient=%s/%s, unit=%s), so no "
                    "doorbell was fired",
                    call.id,
                    call.building_id,
                    call.recipient_type,
                    call.recipient_id,
                    call.unit_id,
                )
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


class ButterflyMXAccessLogCoordinator(DataUpdateCoordinator[dict[int, AccessLogEntry]]):
    """Polls the access log and announces doors that were opened.

    ``data`` maps a tenant ID to the most recent door release for that tenant.

    Deliberately slower than the call loop.  A visitor at the door is a call and
    has its own fast path; this is the record of doors having been opened, which
    is worth knowing about but not worth doubling the request count for.
    """

    config_entry: ButterflyMXConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ButterflyMXClient,
        topology_coordinator: ButterflyMXTopologyCoordinator,
    ) -> None:
        """Initialize the access log coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} access log",
            update_interval=timedelta(seconds=ACCESS_LOG_SCAN_INTERVAL),
        )
        self.client = client
        self.topology_coordinator = topology_coordinator
        self._seen_ids: dict[int, None] = {}
        self._since: datetime | None = None
        self._listeners_for_releases: list[
            Callable[[Tenant, AccessLogEntry], None]
        ] = []
        # As with calls: the first poll marks what already happened as seen
        # rather than announcing a door that was opened before Home Assistant
        # started.
        self._priming = True

    @callback
    def async_add_release_listener(
        self, listener: Callable[[Tenant, AccessLogEntry], None]
    ) -> Callable[[], None]:
        """Register a callback fired for each newly observed door release."""
        self._listeners_for_releases.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners_for_releases:
                self._listeners_for_releases.remove(listener)

        return _remove

    async def _async_update_data(self) -> dict[int, AccessLogEntry]:
        """Poll every building the user has a tenancy in."""
        topology = self.topology_coordinator.data
        if topology is None:
            return dict(self.data or {})

        since = self._since or dt_util.utcnow() - timedelta(
            seconds=ACCESS_LOG_LOOKBACK
        )
        poll_started = dt_util.utcnow()

        entries: list[AccessLogEntry] = []
        try:
            for building_id in topology.building_ids:
                entries.extend(
                    await self.client.async_get_access_logs(building_id, since=since)
                )
        except ButterflyMXAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ButterflyMXConnectionError as err:
            raise UpdateFailed(f"Could not reach ButterflyMX: {err}") from err
        except ButterflyMXError as err:
            raise UpdateFailed(str(err)) from err

        self._since = poll_started - timedelta(seconds=CALL_POLL_OVERLAP)
        data = self._process(entries, topology)
        self._priming = False
        return data

    def _process(
        self, entries: list[AccessLogEntry], topology: ButterflyMXTopology
    ) -> dict[int, AccessLogEntry]:
        """Deduplicate, announce and index new releases by tenant."""
        latest: dict[int, AccessLogEntry] = dict(self.data or {})

        fresh = [entry for entry in entries if entry.id not in self._seen_ids]
        fresh.sort(
            key=lambda e: (e.logged_at or dt_util.utc_from_timestamp(0), e.id)
        )

        for entry in fresh:
            self._remember(entry.id)
            entry = _name_the_tool(entry, topology)
            tenant = self._match_tenant(entry, topology)
            if tenant is None:
                _LOGGER.debug(
                    "Ignoring access log entry %s: no matching tenant", entry.id
                )
                continue

            previous = latest.get(tenant.id)
            if previous is not None and entry.id < previous.id:
                continue
            latest[tenant.id] = entry

            if self._priming:
                continue

            self.hass.bus.async_fire(
                EVENT_DOOR_RELEASE,
                {
                    "entry_id": self.config_entry.entry_id,
                    "tenant_id": tenant.id,
                    "unit_label": tenant.unit_label,
                    **entry.as_event_data(),
                },
            )
            for listener in list(self._listeners_for_releases):
                listener(tenant, entry)

        return latest

    def _remember(self, entry_id: int) -> None:
        """Record an entry ID, evicting the oldest once the cache is full."""
        self._seen_ids[entry_id] = None
        while len(self._seen_ids) > _SEEN_CALL_LIMIT:
            self._seen_ids.pop(next(iter(self._seen_ids)))

    @staticmethod
    def _match_tenant(
        entry: AccessLogEntry, topology: ButterflyMXTopology
    ) -> Tenant | None:
        """Find the tenancy a door release belongs to."""
        if entry.tenant_id is not None:
            for tenant in topology.tenants:
                if tenant.id == entry.tenant_id:
                    return tenant
        return topology.tenant_for_unit(entry.unit_id)


def _name_the_tool(
    entry: AccessLogEntry, topology: ButterflyMXTopology
) -> AccessLogEntry:
    """Replace ``access_tool`` with what the tool actually is.

    Done once here rather than at each place it is displayed, so the event, the
    bus event and the sensor all read the same without any of them having to
    reach for the topology.  The ID is kept alongside, since that is what an
    automation would match on if it wants to be exact.

    Nine door openings in ten look like this, so it is the difference between a
    log of "access_tool 8432576" and a log of "PIN".
    """
    if entry.entry_method != "access_tool":
        return entry
    tool = topology.access_tool(entry.access_tool_id)
    if tool is None:
        return entry
    return replace(entry, entry_method=tool.label)


class ButterflyMXPassCoordinator(DataUpdateCoordinator[dict[int, Pass]]):
    """Keeps track of the visitor and delivery passes on the account.

    ``data`` maps a keychain ID to the pass it describes.

    Polled slowly on purpose.  Passes do not appear on their own: something has
    to create one, and the only two things that can are this integration and the
    ButterflyMX app.  Both are covered without a fast loop, because every
    service that changes a pass asks for a refresh straight afterwards, so the
    interval is really only there to notice edits made in the app.  Usage counts
    lag by up to that interval, which is fine: a door actually being opened
    already arrives on the access log's own loop.

    Two requests per poll, and the second one is the sensitive one -- listing
    virtual keys returns live PINs and QR links.  Nothing here writes them
    anywhere; see :class:`~custom_components.butterflymx.models.VirtualKey`.
    """

    config_entry: ButterflyMXConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ButterflyMXClient,
    ) -> None:
        """Initialize the pass coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} passes",
            update_interval=timedelta(seconds=PASS_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> dict[int, Pass]:
        """List every pass and attach the codes each one has issued."""
        try:
            keychains = await self.client.async_get_keychains()
            # One unfiltered request rather than one per keychain: the accounts
            # this runs on have a handful of passes, and asking per keychain
            # would turn a quiet loop into N+1 requests.
            keys = await self.client.async_get_virtual_keys() if keychains else []
        except ButterflyMXAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ButterflyMXConnectionError as err:
            raise UpdateFailed(f"Could not reach ButterflyMX: {err}") from err
        except ButterflyMXError as err:
            raise UpdateFailed(str(err)) from err

        by_keychain: dict[int, list[VirtualKey]] = {}
        for key in keys:
            if key.keychain_id is not None:
                by_keychain.setdefault(key.keychain_id, []).append(key)

        return {
            keychain.id: Pass(
                keychain=keychain, keys=tuple(by_keychain.get(keychain.id, ()))
            )
            for keychain in keychains
        }

    def passes_for_tenant(self, tenant: Tenant) -> list[Pass]:
        """Return this tenancy's passes, soonest to expire first.

        A keychain carries both a tenant and a unit, and the app can create one
        against either, so both are checked before deciding a pass is somebody
        else's.
        """
        unit_id = tenant.unit.id if tenant.unit else None
        matches = [
            record
            for record in (self.data or {}).values()
            if record.keychain.tenant_id == tenant.id
            or (unit_id is not None and record.keychain.unit_id == unit_id)
        ]
        matches.sort(key=lambda record: (record.keychain.ends_at or _FAR_FUTURE))
        return matches
