"""Snapshot image entity for the most recent ButterflyMX call.

The call log returns a still image URL for each call.  Exposing it as an image
entity rather than a camera matches what the API actually offers: there is no
live stream in the REST API.  Real-time video lives in ButterflyMX's mobile SDK.
"""

from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ButterflyMXConfigEntry
from .const import DOMAIN
from .coordinator import ButterflyMXCallCoordinator
from .entity import ButterflyMXCallEntity
from .models import Tenant

_LOGGER = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a snapshot image entity per tenancy."""
    runtime = entry.runtime_data
    topology_coordinator = runtime.topology
    created: set[int] = set()

    @callback
    def _async_add_new_entities() -> None:
        topology = topology_coordinator.data
        if topology is None:
            return
        new_entities = []
        for tenant in topology.tenants:
            if tenant.id in created:
                continue
            created.add(tenant.id)
            new_entities.append(
                ButterflyMXCallSnapshot(hass, runtime.calls, tenant)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(topology_coordinator.async_add_listener(_async_add_new_entities))


class ButterflyMXCallSnapshot(ButterflyMXCallEntity, ImageEntity):
    """Still image captured by the intercom on the most recent call."""

    _attr_translation_key = "last_call_snapshot"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ButterflyMXCallCoordinator,
        tenant: Tenant,
    ) -> None:
        """Initialize the snapshot entity."""
        # ImageEntity needs hass at construction time, so both parents are
        # initialized explicitly rather than through a single super() call.
        ButterflyMXCallEntity.__init__(self, coordinator, tenant)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{DOMAIN}_tenant_{tenant.id}_last_call_snapshot"
        self._cached_url: str | None = None
        self._cached_image: bytes | None = None
        self._apply_latest_call()

    @callback
    def _apply_latest_call(self) -> None:
        """Update the image timestamp when a newer call arrives."""
        call = self._latest_call
        if call is None or not call.image_url:
            return
        if call.image_url != self._cached_url:
            self._cached_image = None
        self._attr_image_last_updated = call.logged_at or dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh state when the call coordinator reports a new call."""
        self._apply_latest_call()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the snapshot bytes, downloading them at most once."""
        call = self._latest_call
        if call is None or not call.image_url:
            return None
        if self._cached_image is not None and self._cached_url == call.image_url:
            return self._cached_image

        data = await self.coordinator.client.async_fetch_image(call.image_url)
        if data is None:
            return None

        self._cached_url = call.image_url
        self._cached_image = data
        self._attr_content_type = (
            "image/png" if data.startswith(_PNG_MAGIC) else "image/jpeg"
        )
        return data
