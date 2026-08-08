"""Tests for the ButterflyMX diagnostics dump."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def _dump(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[dict, str]:
    """Return the diagnostics payload and the JSON Home Assistant would serve."""
    payload = await async_get_config_entry_diagnostics(hass, entry)
    # Home Assistant serves diagnostics through this encoder, so anything that
    # cannot go through it is a failed download rather than a useful dump.
    return payload, json.dumps(payload, cls=ExtendedJSONEncoder)


@pytest.mark.parametrize(
    "secret",
    [
        "client-id",  # the client the entry authorized as
        "access-1",  # access token
        "refresh-1",  # refresh token
        "ada@example.com",  # who the resident is
        "Ada",
        "Lovelace",
        "Crimson",  # where they live
        "4B",
        "SN-1",  # hardware serials
        "SN-2",
    ],
)
async def test_diagnostics_redacts_secrets_and_pii(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry, secret: str
) -> None:
    """A dump gets attached to bug reports, so none of this may be in it."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    _, dumped = await _dump(hass, config_entry)

    assert secret not in dumped


async def test_diagnostics_keeps_what_makes_it_useful(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Redaction must not take the debuggable parts with it."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    payload, dumped = await _dump(hass, config_entry)

    # Door names say which door misbehaved, and they describe hardware, not people.
    assert "Front Entrance" in dumped
    # IDs are what correlate a dump against the logs.
    assert payload["topology"]["tenants"][0]["building_id"]
    assert payload["topology"]["tenants"][0]["unit"]["id"]
    assert payload["entry"]["data"]["api_url"]


async def test_diagnostics_for_an_entry_that_never_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A dump is wanted most when setup failed, so it must not need runtime data."""
    config_entry.add_to_hass(hass)

    payload, dumped = await _dump(hass, config_entry)

    assert payload["state"] == "not loaded"
    # Redaction has to survive the early return, not just the full dump.
    assert "client-id" not in dumped
    assert "refresh-1" not in dumped
