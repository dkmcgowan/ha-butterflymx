"""Diagnostics support for ButterflyMX."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ButterflyMXConfigEntry
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_INTEGRATION_IDS,
)

# A diagnostics dump usually ends up attached to a bug report, so it has to be
# safe to hand to a stranger.
TO_REDACT = {
    # Credentials and anything that acts like one.  Anybody holding the webhook
    # ID can post fake doorbell events, and the integration IDs are live handles
    # on the account's registrations; "webhook_enabled" says all that is needed.
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_INTEGRATION_IDS,
    "access_token",
    "refresh_token",
    # A snapshot URL is pre-signed, so the link is the picture.
    "image_url",
    "serial_number",
    # Things that open a door.  None of these is parsed into a model today --
    # AccessTool drops the PIN it is sent, and passes never reach diagnostics --
    # so this is here to make sure that stays true if one of them ever is.
    "code",
    "pin_code",
    "qr_code_url",
    "instructions_url",
    # Who the resident is.
    "email",
    "first_name",
    "last_name",
    "full_name",
    # Where they live.  Redacting the names but not the address would be a
    # strange place to stop: building plus unit plus floor is a home address.
    # building_id and unit_id survive, which is what correlating logs needs.
    "building_name",
    "label",
    "floor",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ButterflyMXConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    result: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            # Only polling intervals and a boolean, nothing to hide.
            "options": dict(entry.options),
        },
    }

    # Diagnostics are wanted most when something is wrong, and that includes a
    # setup that never finished and so never attached its runtime data.  Report
    # what there is rather than failing the download.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        result["state"] = "not loaded"
        return result

    topology = runtime.topology.data
    result["topology"] = async_redact_data(
        asdict(topology) if topology is not None else {}, TO_REDACT
    )
    result["calls"] = async_redact_data(
        {
            str(tenant_id): asdict(call)
            for tenant_id, call in (runtime.calls.data or {}).items()
        },
        TO_REDACT,
    )
    result["access_log"] = async_redact_data(
        {
            str(tenant_id): asdict(entry)
            for tenant_id, entry in (runtime.access_log.data or {}).items()
        },
        TO_REDACT,
    )
    result["webhook_enabled"] = runtime.webhook is not None
    return result
