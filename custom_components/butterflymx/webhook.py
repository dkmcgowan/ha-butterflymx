"""Optional webhook push support.

Polling the call log is the default because it works for every install.  If Home
Assistant is reachable from the internet, ButterflyMX can push call events
instead, which removes the polling latency.

This is experimental: ButterflyMX documents the webhook registration endpoints
and the general shape of the payload, but not an exact schema, so the parser
below is deliberately forgiving and logs anything it cannot interpret.
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
)
from .exceptions import ButterflyMXError
from .models import Call

_LOGGER = logging.getLogger(__name__)


class ButterflyMXWebhookManager:
    """Registers a Home Assistant webhook and mirrors it in ButterflyMX."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        """Initialise the manager."""
        self.hass = hass
        self.entry = entry
        self._webhook_id: str | None = None
        self._registered_ids: dict[str, str] = {}

    async def async_setup(self) -> None:
        """Register the webhook locally and with ButterflyMX."""
        try:
            base_url = get_url(self.hass, allow_internal=False, prefer_external=True)
        except NoURLAvailableError:
            _LOGGER.warning(
                "Webhook push is enabled but Home Assistant has no external URL "
                "configured; falling back to polling"
            )
            return

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
            return

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
                    tenant.id, target_url, [WEBHOOK_RESOURCE_CALL]
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
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_WEBHOOK_INTEGRATION_IDS: {}},
        )

    async def _async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        """Handle an inbound ButterflyMX event."""
        try:
            payload = await request.json()
        except ValueError:
            _LOGGER.debug("Ignoring ButterflyMX webhook with a non-JSON body")
            return Response(status=200)

        _LOGGER.debug("ButterflyMX webhook payload: %s", payload)
        runtime = getattr(self.entry, "runtime_data", None)

        # Webhook bodies do not reliably carry a building_id; when the account
        # only covers one building there is no ambiguity to resolve.
        default_building_id: int | None = None
        if runtime is not None and runtime.topology.data is not None:
            building_ids = runtime.topology.data.building_ids
            if len(building_ids) == 1:
                default_building_id = building_ids[0]

        call = parse_call_payload(payload, default_building_id)
        if call is None:
            return Response(status=200)

        if runtime is not None:
            await runtime.calls.async_handle_pushed_call(call)
        return Response(status=200)


def parse_call_payload(payload: Any, default_building_id: int | None = None) -> Call | None:
    """Pull a Call out of a webhook body, whatever shape it arrives in.

    Handles a bare call object, a JSON:API style ``data.attributes`` envelope,
    and a ``resource``/``resource_type`` wrapper.  Returns ``None`` for anything
    that is not a call.
    """
    if not isinstance(payload, dict):
        return None

    resource_type = payload.get("resource_type") or payload.get("type")
    body: Any = payload

    data = payload.get("data")
    if isinstance(data, dict):
        resource_type = data.get("resource_type") or data.get("type") or resource_type
        attributes = data.get("attributes")
        body = attributes if isinstance(attributes, dict) else data

    for key in ("call", "resource", "payload"):
        nested = body.get(key) if isinstance(body, dict) else None
        if isinstance(nested, dict):
            body = nested
            resource_type = resource_type or key
            break

    if resource_type and str(resource_type).lower() not in {
        WEBHOOK_RESOURCE_CALL,
        "calls",
        "integrations",
    }:
        return None
    if not isinstance(body, dict):
        return None

    return Call.from_api(body, default_building_id)
