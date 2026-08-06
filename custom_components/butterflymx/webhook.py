"""Optional webhook push support.

Polling the call log is the default because it works for every install.  If Home
Assistant is reachable from the internet, ButterflyMX can push call events
instead, which removes the polling latency.

This is experimental.  ButterflyMX documents the webhook registration endpoints
and the general shape of the payload but not an exact schema, so the parser below
accepts several shapes and logs anything it cannot interpret.
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
from .models import ButterflyMXTopology, Call

_LOGGER = logging.getLogger(__name__)

# Resource types that mean "somebody called the unit".  Deliveries about
# anything else, including integrations themselves, are not doorbells.
CALL_RESOURCE_TYPES = frozenset({WEBHOOK_RESOURCE_CALL, "calls"})


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
        """Handle an inbound ButterflyMX event."""
        try:
            payload = await request.json()
        except ValueError:
            _LOGGER.debug("Ignoring ButterflyMX webhook with a non-JSON body")
            return Response(status=200)

        _LOGGER.debug("ButterflyMX webhook payload: %s", payload)
        runtime = getattr(self.entry, "runtime_data", None)

        resource_type, body = unwrap_event(payload)
        if not is_call_event(resource_type):
            # Only calls are subscribed to, so anything else is somebody else's
            # registration reaching us and is not worth complaining about.
            _LOGGER.debug("Ignoring ButterflyMX %s webhook", resource_type)
            return Response(status=200)

        topology = runtime.topology.data if runtime is not None else None
        call = parse_call_payload(payload, building_id_for_event(body, topology))
        if call is None:
            # Answer 200 regardless: a non-200 has ButterflyMX retry, and a
            # payload we cannot read will not read any better the second time.
            _LOGGER.warning(
                "Ignoring a ButterflyMX webhook that could not be read as a "
                "call; the doorbell did not fire for it. Payload: %s",
                payload,
            )
            return Response(status=200)

        if runtime is not None:
            await runtime.calls.async_handle_pushed_call(call)
        return Response(status=200)


def _identifier(body: dict[str, Any], *names: str) -> int | None:
    """Read an ID that may arrive bare or wrapped in an object.

    The one documented delivery carries ``"access_point": 22177636`` rather than
    a nested object, while the REST API nests the same things, so both are
    accepted.
    """
    for name in names:
        value: Any = body.get(name)
        if isinstance(value, dict):
            value = value.get("id")
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def building_id_for_event(
    body: dict[str, Any] | None, topology: ButterflyMXTopology | None
) -> int | None:
    """Work out which building a pushed call belongs to.

    Deliveries carry no building_id -- the documented example has an access
    point and nothing above it -- so it has to be inferred.  One building means
    there is nothing to infer.  Otherwise whatever the payload does identify is
    looked up in the topology.

    These are assumptions.  No call delivery has been seen against a live
    account, so which of these fields actually appears is unconfirmed; see
    REVIEW.local.md.  Returning None is safe: the call is dropped with a warning
    rather than attributed to the wrong building, and polling still finds it.
    """
    if topology is None:
        return None

    building_ids = topology.building_ids
    if len(building_ids) == 1:
        return building_ids[0]
    if not isinstance(body, dict):
        return None

    access_point_id = _identifier(body, "access_point", "access_point_id")
    if access_point_id is not None:
        for point in topology.access_points:
            if point.id == access_point_id:
                return point.building_id

    device_id = _identifier(body, "device", "device_id")
    if device_id is not None:
        for device in topology.devices:
            if device.id == device_id:
                return device.building_id
        for point in topology.access_points:
            if device_id in point.device_ids:
                return point.building_id

    unit_id = _identifier(body, "unit", "unit_id")
    if unit_id is not None:
        tenant = topology.tenant_for_unit(unit_id)
        if tenant is not None:
            return tenant.building_id

    return None


def unwrap_event(payload: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Peel the delivery envelope off, returning its resource type and body.

    ButterflyMX documents deliveries as ``{"event": {"resource_type", "action",
    "data"}}``.  The other shapes handled here are not documented and are kept
    only because the exact envelope has never been seen against a live account;
    they cost nothing and none of them can match the documented one.
    """
    if not isinstance(payload, dict):
        return None, None

    event = payload.get("event")
    if isinstance(event, dict):
        payload = event

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

    return (
        str(resource_type).lower() if resource_type else None,
        body if isinstance(body, dict) else None,
    )


def is_call_event(resource_type: str | None) -> bool:
    """Return True when a delivery is about a call.

    An unlabelled body is assumed to be a call, since a call is the only thing
    this integration subscribes to.
    """
    return resource_type is None or resource_type in CALL_RESOURCE_TYPES


def parse_call_payload(payload: Any, default_building_id: int | None = None) -> Call | None:
    """Pull a Call out of a webhook body, whatever shape it arrives in."""
    resource_type, body = unwrap_event(payload)
    if body is None or not is_call_event(resource_type):
        return None
    return Call.from_api(body, default_building_id)
