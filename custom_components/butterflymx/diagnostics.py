"""Diagnostics support for ButterflyMX."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ButterflyMXConfigEntry
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_TOKEN, CONF_WEBHOOK_ID

TO_REDACT = {
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    "access_token",
    "refresh_token",
    "email",
    "first_name",
    "last_name",
    "full_name",
    "image_url",
    "serial_number",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ButterflyMXConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    topology = runtime.topology.data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "topology": async_redact_data(
            asdict(topology) if topology is not None else {}, TO_REDACT
        ),
        "calls": async_redact_data(
            {
                str(tenant_id): asdict(call)
                for tenant_id, call in (runtime.calls.data or {}).items()
            },
            TO_REDACT,
        ),
        "webhook_enabled": runtime.webhook is not None,
    }
