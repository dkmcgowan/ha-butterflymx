"""The ButterflyMX integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ButterflyMXClient
from .auth import ButterflyMXAuth
from .const import (
    CONF_ACCOUNTS_URL,
    CONF_API_URL,
    CONF_CALL_SCAN_INTERVAL,
    CONF_CLIENT_ID,
    CONF_ENABLE_WEBHOOK,
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    DEFAULT_CALL_SCAN_INTERVAL,
    DEFAULT_RELOCK_DELAY,
    WEBHOOK_FALLBACK_SCAN_INTERVAL,
)
from .coordinator import (
    ButterflyMXAccessLogCoordinator,
    ButterflyMXCallCoordinator,
    ButterflyMXPassCoordinator,
    ButterflyMXTopologyCoordinator,
)
from .entity import building_device_info
from .webhook import ButterflyMXWebhookManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
]


@dataclass
class ButterflyMXRuntimeData:
    """Objects shared between the platforms of one config entry."""

    client: ButterflyMXClient
    topology: ButterflyMXTopologyCoordinator
    calls: ButterflyMXCallCoordinator
    access_log: ButterflyMXAccessLogCoordinator
    passes: ButterflyMXPassCoordinator
    relock_delay: int
    options_snapshot: dict[str, Any] = field(default_factory=dict)
    webhook: ButterflyMXWebhookManager | None = None


type ButterflyMXConfigEntry = ConfigEntry[ButterflyMXRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: ButterflyMXConfigEntry) -> bool:
    """Set up ButterflyMX from a config entry."""
    session = async_get_clientsession(hass)

    async def _async_save_token(token: dict[str, Any]) -> None:
        """Persist a refreshed token back onto the config entry."""
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token}
        )

    auth = ButterflyMXAuth(
        session=session,
        accounts_url=entry.data[CONF_ACCOUNTS_URL],
        client_id=entry.data[CONF_CLIENT_ID],
        token=entry.data[CONF_TOKEN],
        token_updater=_async_save_token,
    )
    client = ButterflyMXClient(session, entry.data[CONF_API_URL], auth)

    # No try/except: the coordinator converts its own failures into
    # ConfigEntryAuthFailed or ConfigEntryNotReady, which is what Home Assistant
    # wants to see here.
    topology = ButterflyMXTopologyCoordinator(hass, entry, client)
    await topology.async_config_entry_first_refresh()

    scan_interval = entry.options.get(CONF_CALL_SCAN_INTERVAL, DEFAULT_CALL_SCAN_INTERVAL)
    calls = ButterflyMXCallCoordinator(hass, entry, client, topology, scan_interval)
    # Prime the call log without announcing everything that happened while Home
    # Assistant was down; the first poll only marks existing calls as seen.
    #
    # This has to finish before the webhook is registered below.  Priming
    # deliberately swallows the calls it sees, so a push arriving first would be
    # marked seen, never announced, and then skipped by the poll as a duplicate:
    # a doorbell that rings and does nothing.  Keep this ordering.
    await calls.async_config_entry_first_refresh()

    access_log = ButterflyMXAccessLogCoordinator(hass, entry, client, topology)
    # Primed the same way as calls: mark what already happened as seen so a
    # door opened before Home Assistant started is not announced now.
    await access_log.async_config_entry_first_refresh()

    passes = ButterflyMXPassCoordinator(hass, entry, client)
    # Refreshed rather than first-refreshed: a failure here must not stop setup.
    # Visitor passes are a convenience, and losing them is no reason to lose the
    # doorbell and the doors as well.  The sensor reports itself unavailable and
    # the next poll picks it up.
    await passes.async_refresh()

    entry.runtime_data = ButterflyMXRuntimeData(
        client=client,
        topology=topology,
        calls=calls,
        access_log=access_log,
        passes=passes,
        relock_delay=entry.options.get(CONF_RELOCK_DELAY, DEFAULT_RELOCK_DELAY),
        options_snapshot=dict(entry.options),
    )

    _async_register_buildings(hass, entry, topology)

    if entry.options.get(CONF_ENABLE_WEBHOOK, False):
        manager = ButterflyMXWebhookManager(hass, entry)
        # Assigned before setup runs so a half-finished registration is still
        # torn down on unload.
        entry.runtime_data.webhook = manager
        try:
            pushing = await manager.async_setup()
        # Broad on purpose: push is an optional extra, and no failure in it is
        # worth taking down an integration that works fine without it.
        except Exception:
            _LOGGER.exception(
                "Could not set up ButterflyMX webhook push; continuing with "
                "polling, which is how calls are noticed by default anyway"
            )
            pushing = False

        if pushing:
            # Push makes the doorbell immediate, so polling stops being how a
            # call is noticed.  It does not stop: a delivery that lands while
            # Home Assistant is restarting is gone for good, ButterflyMX
            # promises no replay, and a registration whose URL has gone stale
            # fails silently.  A slow poll is what catches all three.
            #
            # Nothing is scheduled yet: the coordinator arms its timer when the
            # first entity subscribes, in async_forward_entry_setups below.
            fallback = timedelta(seconds=WEBHOOK_FALLBACK_SCAN_INTERVAL)
            calls.update_interval = fallback
            # Door releases are pushed too, so the access log gets the same
            # treatment, unless it was already polling more slowly than this.
            access_log.update_interval = max(access_log.update_interval, fallback)
            _LOGGER.debug(
                "Webhook push registered; polling slowed to %ss and kept as a "
                "safety net",
                WEBHOOK_FALLBACK_SCAN_INTERVAL,
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


@callback
def _async_register_buildings(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    topology: ButterflyMXTopologyCoordinator,
) -> None:
    """Create the building devices the doors and units hang off.

    Registered here rather than by an entity, because a building has no entity
    of its own: it exists to group the doors and the intercom under a name.
    Doing it before the platforms load also means the ``via_device`` links they
    declare resolve, instead of Home Assistant reporting a parent that does not
    exist.

    One per tenancy, not one per building.  A tenancy is the thing that gets a
    building device pointed at it, and an account with two tenancies in one
    building would otherwise leave the second one's unit orphaned.
    """
    registry = dr.async_get(hass)
    for tenant in (topology.data.tenants if topology.data else []):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, **building_device_info(tenant)
        )


async def async_unload_entry(hass: HomeAssistant, entry: ButterflyMXConfigEntry) -> bool:
    """Unload a config entry."""
    runtime: ButterflyMXRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.webhook is not None:
        await runtime.webhook.async_teardown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(
    hass: HomeAssistant, entry: ButterflyMXConfigEntry
) -> None:
    """Reload the entry when the user changes options.

    Entry updates are not always option changes, and reloading on all of them
    would be wrong twice over.  A refreshed OAuth token is written back to the
    entry roughly once a day.  More sharply, tearing down the webhook writes to
    the entry during unload, and this listener is still attached at that point
    because async_on_unload callbacks do not run until async_unload_entry has
    returned -- so an unconditional reload would schedule a reload of the entry
    being unloaded.  Comparing against the options we started with avoids both.
    """
    runtime: ButterflyMXRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None and dict(entry.options) == runtime.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)
