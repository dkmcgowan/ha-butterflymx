"""The ButterflyMX integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ButterflyMXClient
from .auth import ButterflyMXAuth
from .const import (
    CONF_ACCOUNTS_URL,
    CONF_API_URL,
    CONF_CALL_SCAN_INTERVAL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_WEBHOOK,
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    DEFAULT_CALL_SCAN_INTERVAL,
    DEFAULT_RELOCK_DELAY,
)
from .coordinator import ButterflyMXCallCoordinator, ButterflyMXTopologyCoordinator
from .exceptions import ButterflyMXAuthError, ButterflyMXConnectionError
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
    relock_delay: int
    options_snapshot: dict[str, Any] = field(default_factory=dict)
    webhook: ButterflyMXWebhookManager | None = None
    known_lock_keys: set[str] = field(default_factory=set)
    known_tenant_ids: set[int] = field(default_factory=set)


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
        client_secret=entry.data[CONF_CLIENT_SECRET],
        token=entry.data[CONF_TOKEN],
        token_updater=_async_save_token,
    )
    client = ButterflyMXClient(session, entry.data[CONF_API_URL], auth)

    topology = ButterflyMXTopologyCoordinator(hass, entry, client)
    try:
        await topology.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except ConfigEntryNotReady:
        raise
    except ButterflyMXAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ButterflyMXConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    scan_interval = entry.options.get(CONF_CALL_SCAN_INTERVAL, DEFAULT_CALL_SCAN_INTERVAL)
    calls = ButterflyMXCallCoordinator(hass, entry, client, topology, scan_interval)
    # Prime the call log without announcing everything that happened while Home
    # Assistant was down; the first poll only marks existing calls as seen.
    await calls.async_config_entry_first_refresh()

    entry.runtime_data = ButterflyMXRuntimeData(
        client=client,
        topology=topology,
        calls=calls,
        relock_delay=entry.options.get(CONF_RELOCK_DELAY, DEFAULT_RELOCK_DELAY),
        options_snapshot=dict(entry.options),
    )

    if entry.options.get(CONF_ENABLE_WEBHOOK, False):
        manager = ButterflyMXWebhookManager(hass, entry)
        entry.runtime_data.webhook = manager
        await manager.async_setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


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

    This listener also fires when a refreshed OAuth token is written back to the
    entry, so compare against the options we set up with instead of reloading on
    every entry update.
    """
    runtime: ButterflyMXRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None and dict(entry.options) == runtime.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)
