"""Optional webhook push support.

Polling the call log is the default because it works for every install.  If Home
Assistant is reachable from the internet, ButterflyMX can push instead, which
takes the latency out of the doorbell.

A delivery is treated as a nudge, not as data.  The body is never parsed, and
that is a deliberate decision made after seeing real ones:

* It carries no call id.  There is a ``guid``, but the REST API knows calls by
  an integer ``id`` and returns no guid, so the two cannot be matched up.  A
  pushed call and the same call arriving on the next poll would look like two
  different visitors and ring the doorbell twice.
* Its other identifiers belong to a different ID space.  For one real call the
  webhook said panel 28516 and user 3220259 where the REST API said device 58805
  and tenant 8304247.  Only the unit ID agrees.
* It says nothing about what happened.  Two deliveries, one call answered and
  one left to time out, differed only by guid and image URL.  REST reports
  ``opened_door`` against ``timeout_online_signal``, plus a timestamp and a
  status the payload simply does not have.
* It carries the resident's name, email and SIP username, none of which this
  integration wants to touch.

So the delivery only says "go and look", and the logs stay the single source of
truth.  A real delivery arrives in the same second the call is logged, so the
record is already there to be found.

Both call and door_release deliveries are subscribed to.  Both are handled the
same way, since the body is not read: refresh the call log and the access log,
and let those say what actually happened.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_INTEGRATION_IDS,
    DOMAIN,
    WEBHOOK_RESOURCE_CALL,
    WEBHOOK_RESOURCE_DOOR_RELEASE,
)
from .exceptions import ButterflyMXError

_LOGGER = logging.getLogger(__name__)


class ButterflyMXWebhookManager:
    """Registers a Home Assistant webhook and mirrors it in ButterflyMX."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self._webhook_id: str | None = None
        self._registered_ids: dict[str, str] = {}

    async def async_setup(self) -> bool:
        """Register the webhook locally and with ButterflyMX.

        Returns True only if ButterflyMX has somewhere to push to, meaning at
        least one tenancy is registered.  Anything less and calls still arrive
        by polling alone, so the caller needs to know not to slow it down.
        """
        try:
            base_url = get_url(self.hass, allow_internal=False, prefer_external=True)
        except NoURLAvailableError:
            _LOGGER.warning(
                "Webhook push is enabled but Home Assistant has no external URL "
                "configured; falling back to polling"
            )
            return False

        webhook_id = self.entry.data.get(CONF_WEBHOOK_ID) or webhook.async_generate_id()
        if webhook_id != self.entry.data.get(CONF_WEBHOOK_ID):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_WEBHOOK_ID: webhook_id}
            )
        self._webhook_id = webhook_id

        webhook.async_register(
            self.hass, DOMAIN, "ButterflyMX", webhook_id, self._async_handle_webhook
        )

        target_url = f"{base_url}{webhook.async_generate_path(webhook_id)}"
        client = self.entry.runtime_data.client
        topology = self.entry.runtime_data.topology.data
        if topology is None:
            return False

        stored: dict[str, str] = dict(
            self.entry.data.get(CONF_WEBHOOK_INTEGRATION_IDS) or {}
        )
        for tenant in topology.tenants:
            key = str(tenant.id)
            if key in stored:
                self._registered_ids[key] = stored[key]
                continue
            try:
                created = await client.async_create_tenant_integration(
                    tenant.id,
                    target_url,
                    [WEBHOOK_RESOURCE_CALL, WEBHOOK_RESOURCE_DOOR_RELEASE],
                )
            except ButterflyMXError as err:
                _LOGGER.warning(
                    "Could not register ButterflyMX webhook for tenant %s: %s",
                    tenant.id,
                    err,
                )
                continue
            integration_id = str(created.get("id") or created.get("guid") or "")
            if integration_id:
                self._registered_ids[key] = integration_id

        if self._registered_ids != stored:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    CONF_WEBHOOK_INTEGRATION_IDS: dict(self._registered_ids),
                },
            )

        if not self._registered_ids:
            _LOGGER.warning(
                "Webhook push is enabled but no ButterflyMX tenancy accepted a "
                "registration; calls will only be noticed by polling"
            )
        return bool(self._registered_ids)

    async def async_teardown(self) -> None:
        """Unregister the webhook and remove it from ButterflyMX."""
        if self._webhook_id is not None:
            webhook.async_unregister(self.hass, self._webhook_id)
            self._webhook_id = None

        runtime = getattr(self.entry, "runtime_data", None)
        if runtime is None:
            return
        for tenant_id, integration_id in list(self._registered_ids.items()):
            try:
                await runtime.client.async_delete_tenant_integration(
                    int(tenant_id), integration_id
                )
            except (ButterflyMXError, ValueError) as err:
                _LOGGER.debug(
                    "Could not remove ButterflyMX webhook %s: %s", integration_id, err
                )
        self._registered_ids.clear()
        # Only write when there is something to clear, so an unload that had no
        # registrations does not touch the entry at all.
        if self.entry.data.get(CONF_WEBHOOK_INTEGRATION_IDS):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_WEBHOOK_INTEGRATION_IDS: {}},
            )

    async def _async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        """Take a delivery as a signal to go and read the call log.

        The body is not parsed; see the module docstring for why.  Always
        answers 200: a non-200 asks ButterflyMX to deliver again, and there is
        nothing here that a second attempt would fix.
        """
        runtime = getattr(self.entry, "runtime_data", None)
        if runtime is None:
            return Response(status=200)

        # Both logs are read, because the body is not inspected and so nothing
        # here says which kind of thing happened.  Two cheap reads on an event
        # that happens a few times a day is a better trade than parsing a
        # payload whose shape is not dependable.
        #
        # Debounced and immediate: the first delivery refreshes at once, and a
        # burst collapses into one read rather than one read each.
        await runtime.calls.async_request_refresh()
        await runtime.access_log.async_request_refresh()
        return Response(status=200)
