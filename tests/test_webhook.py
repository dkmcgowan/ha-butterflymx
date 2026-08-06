"""Tests for webhook push.

A delivery is a signal, not data. These check that it makes the integration go
and read the call log, and that it does not try to understand the body.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_ENABLE_WEBHOOK,
    DEFAULT_CALL_SCAN_INTERVAL,
    WEBHOOK_FALLBACK_SCAN_INTERVAL,
)
from custom_components.butterflymx.webhook import ButterflyMXWebhookManager

# A real delivery, captured from production. Note what is missing: no call id,
# no timestamp, no building, and nothing saying whether anyone answered.
REAL_DELIVERY = {
    "guid": "ba17520f-8768-4723-88f9-5eaa2c35171a",
    "user": {
        "id": 3220259,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "sip_username": "3077790",
        "email": "ada@example.com",
    },
    "panel": {"id": 28516, "name": "Front Door", "sip_username": "panel_28516"},
    "unit": {"id": 1859622, "label": "4B"},
    "provider": "twilio",
    "source": {"id": 28516, "name": "Front Door", "sip_username": "panel_28516"},
    "image_url": "https://bmx-rails-production.s3.amazonaws.com/cache/052f0c40.jpeg",
}


async def _setup_with_push(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up with push enabled but registration stubbed out."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_WEBHOOK: True})
    with patch.object(ButterflyMXWebhookManager, "async_setup", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def _deliver(hass: HomeAssistant, manager, payload) -> None:
    """Hand a payload to the manager the way the HTTP view would."""
    request = AsyncMock()
    request.json = AsyncMock(return_value=payload)
    await manager._async_handle_webhook(hass, "wh", request)
    await hass.async_block_till_done()


async def test_a_delivery_makes_us_read_the_call_log(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """The body cannot be turned into a call, so it only triggers a refresh.

    The payload carries a guid the REST API does not know, identifiers from a
    different ID space, and no status at all. The call log has all of it, so
    that is what gets read.
    """
    await _setup_with_push(hass, config_entry)
    manager = config_entry.runtime_data.webhook

    with patch.object(
        config_entry.runtime_data.calls, "async_request_refresh", AsyncMock()
    ) as refresh:
        await _deliver(hass, manager, REAL_DELIVERY)

    assert refresh.called


async def test_push_only_slows_polling_once_it_has_proved_itself(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Registering proves nothing; an arriving delivery does.

    ButterflyMX accepting a URL says nothing about whether it can reach this
    host. Slowing down on registration alone would leave an unreachable install
    on five minute doorbells with no clue why.
    """
    await _setup_with_push(hass, config_entry)
    runtime = config_entry.runtime_data

    # Registered, but nothing has arrived yet.
    assert runtime.calls.update_interval == timedelta(seconds=DEFAULT_CALL_SCAN_INTERVAL)

    with patch.object(runtime.calls, "async_request_refresh", AsyncMock()):
        await _deliver(hass, runtime.webhook, REAL_DELIVERY)

    assert runtime.calls.update_interval == timedelta(
        seconds=WEBHOOK_FALLBACK_SCAN_INTERVAL
    )


@pytest.mark.parametrize(
    "payload",
    [REAL_DELIVERY, {}, {"anything": "at all"}, [], "not json either"],
    ids=["real", "empty", "unknown", "list", "string"],
)
async def test_any_body_at_all_is_accepted(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry, payload
) -> None:
    """Nothing is parsed, so nothing about the body can make a delivery fail."""
    await _setup_with_push(hass, config_entry)
    runtime = config_entry.runtime_data

    with patch.object(runtime.calls, "async_request_refresh", AsyncMock()) as refresh:
        await _deliver(hass, runtime.webhook, payload)

    assert refresh.called


async def test_nothing_from_the_payload_is_kept(
    hass: HomeAssistant,
    mock_topology,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivery carries the resident's name, email and SIP username.

    None of it is wanted, and the surest way not to mishandle it is never to
    read it.
    """
    await _setup_with_push(hass, config_entry)
    runtime = config_entry.runtime_data

    with patch.object(runtime.calls, "async_request_refresh", AsyncMock()):
        await _deliver(hass, runtime.webhook, REAL_DELIVERY)

    stored = repr(runtime.calls.data or {})
    for secret in ("ada@example.com", "Lovelace", "3077790"):
        assert secret not in stored
