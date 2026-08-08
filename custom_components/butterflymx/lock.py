"""Lock platform for ButterflyMX doors.

ButterflyMX doors are momentary releases: the API can buzz a door open but it
reports no lock state, and there is no "lock" command.  Each entity therefore
models the door as locked at rest, briefly reports ``unlocked`` after a
successful release, and returns to ``locked`` once the strike has re-engaged.
``assumed_state`` is set so the UI shows discrete open/close controls rather
than a toggle that pretends to know the truth.

``is_open`` is deliberately never reported.  Releasing the strike makes a door
openable, but whether anyone actually pushed it is not something this API can
tell us, so claiming it would be a guess dressed up as a reading.  Unlock and
open therefore send the same request and report the same state; both exist only
so the UI can offer either control.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ButterflyMXConfigEntry
from .const import DOMAIN, DOOR_RELEASE_COOLDOWN, PANEL_COMMAND_OPEN_DOOR
from .coordinator import (
    ButterflyMXCallCoordinator,
    ButterflyMXTopologyCoordinator,
    LockTarget,
    build_lock_targets,
)
from .entity import ButterflyMXTopologyEntity, door_device_info
from .exceptions import ButterflyMXError
from .models import ButterflyMXTopology

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ButterflyMX locks and keep them in sync with the topology."""
    runtime = entry.runtime_data
    coordinator = runtime.topology
    created: set[str] = set()

    @callback
    def _async_add_new_locks() -> None:
        topology: ButterflyMXTopology | None = coordinator.data
        if topology is None:
            return
        new_entities = []
        for target in build_lock_targets(topology):
            if target.unique_key in created:
                continue
            created.add(target.unique_key)
            new_entities.append(
                ButterflyMXLock(
                    coordinator, target, runtime.relock_delay, runtime.calls
                )
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_locks()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_locks))


class ButterflyMXLock(ButterflyMXTopologyEntity, LockEntity):
    """A ButterflyMX door exposed as a Home Assistant lock."""

    _attr_name = None
    _attr_assumed_state = True
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(
        self,
        coordinator: ButterflyMXTopologyCoordinator,
        target: LockTarget,
        relock_delay: int,
        calls: ButterflyMXCallCoordinator,
    ) -> None:
        """Initialize the lock."""
        super().__init__(coordinator)
        self._target = target
        self._relock_delay = relock_delay
        self._calls = calls
        self._attr_unique_id = f"{DOMAIN}_{target.unique_key}"
        self._attr_device_info = door_device_info(target)
        self._attr_is_locked = True
        self._attr_is_unlocking = False
        self._relock_task: asyncio.Task[None] | None = None
        self._release_lock = asyncio.Lock()
        self._last_release: float = 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the ButterflyMX identifiers behind this door."""
        return {
            "building_id": self._target.building_id,
            "tenant_id": self._target.tenant_id,
            "access_point_id": self._target.access_point_id,
            "device_id": self._target.device_id,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Locking is implicit, since the strike re-engages on its own."""
        self._cancel_relock()
        self._set_locked()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Buzz the door open."""
        await self._async_release()

    async def async_open(self, **kwargs: Any) -> None:
        """Buzz the door open.

        The same request as unlocking, reported the same way, because it is the
        same thing happening to the door.  Both exist so the UI can offer either
        control.
        """
        await self._async_release()

    async def _async_release(self) -> None:
        """Send a door release request, guarding against double-fires."""
        async with self._release_lock:
            loop = asyncio.get_running_loop()
            since_last = loop.time() - self._last_release
            if self._last_release and since_last < DOOR_RELEASE_COOLDOWN:
                _LOGGER.warning(
                    "Ignoring door release for %s: another release was sent %.1fs ago",
                    self._target.name,
                    since_last,
                )
                return

            self._attr_is_unlocking = True
            self.async_write_ha_state()

            try:
                await self.coordinator.client.async_release_door(
                    self._target.tenant_id,
                    access_point_id=self._target.access_point_id,
                    device_id=self._target.device_id,
                )
            except ButterflyMXError as err:
                self._set_locked()
                raise HomeAssistantError(
                    f"Could not open {self._target.name}: {err}"
                ) from err

            self._last_release = loop.time()
            self._attr_is_unlocking = False
            self._attr_is_locked = False
            self.async_write_ha_state()
            self._schedule_relock()
            await self._async_tell_the_panel()

    async def _async_tell_the_panel(self) -> None:
        """If a visitor is calling right now, tell the panel they were let in.

        Opening a door and answering a call are two different things to
        ButterflyMX, and v4 only does the first.  Without this the panel carries
        on dialling and rolls over to a phone call, even though the visitor is
        already inside.  The official app sends the same command when you open
        the door from its notification.

        Never fatal.  The door has already opened by this point, which is what
        was asked for, so a failure here is logged and nothing more.
        """
        call = self._calls.live_call_for_tenant(self._target.tenant_id)
        if call is None:
            return
        try:
            handle = await self.coordinator.client.async_get_call_handle(call.id)
            if handle is None:
                return
            await self.coordinator.client.async_notify_panel(
                PANEL_COMMAND_OPEN_DOOR, handle
            )
        except ButterflyMXError as err:
            _LOGGER.warning(
                "Opened %s but could not tell the panel the call was handled, "
                "so it may keep ringing: %s",
                self._target.name,
                err,
            )
        else:
            _LOGGER.debug("Told panel %s that call %s was answered", handle.panel_id, call.id)

    def _schedule_relock(self) -> None:
        """Return the entity to ``locked`` after the strike times out."""
        self._cancel_relock()

        async def _relock() -> None:
            # A cancelled timer means a newer release replaced this one, so let
            # the cancellation propagate rather than reporting the door locked.
            await asyncio.sleep(self._relock_delay)
            self._set_locked()

        self._relock_task = self.hass.async_create_task(
            _relock(), f"{DOMAIN} relock {self._target.unique_key}"
        )

    def _cancel_relock(self) -> None:
        """Cancel a pending relock timer."""
        if self._relock_task is not None and not self._relock_task.done():
            self._relock_task.cancel()
        self._relock_task = None

    @callback
    def _set_locked(self) -> None:
        """Reset the entity to its resting state."""
        self._attr_is_unlocking = False
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel timers when the entity goes away."""
        self._cancel_relock()
        await super().async_will_remove_from_hass()
