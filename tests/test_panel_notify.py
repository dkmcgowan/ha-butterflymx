"""Tests for telling the panel a call has been handled.

Opening a door and answering a call are separate things to ButterflyMX, and v4
only does the first. Release the door and nothing else, and the panel carries on
dialling until it rolls the visitor over to a phone call, which is what happened
on a real install before this existed.

The second half lives in v3, so these tests also pin the request shape: get it
wrong and the failure is a panel that keeps ringing, which no amount of v4
testing would show.
"""

from __future__ import annotations

from homeassistant.components.lock import (
    DOMAIN as LOCK_DOMAIN,
    SERVICE_OPEN,
    LockState,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_RELOCK_DELAY,
    DOMAIN,
    SERVICE_DECLINE_CALL,
)
from custom_components.butterflymx.models import CallHandle

from .conftest import API_URL, call_payload, register_topology

LOCK_ENTITY = "lock.front_entrance"
DOORBELL_ENTITY = "event.unit_4b_doorbell"

CALL_ID = 900001
GUID = "53a93e07-0230-4bb1-aa0b-ef683b882c5c"
PANEL_ID = 28516

V3_CALLS = f"{API_URL}/v3/me/calls"


def v3_call(
    call_id: int = CALL_ID,
    guid: str = GUID,
    panel: int = PANEL_ID,
    status: str = "initializing",
) -> dict:
    """One call as v3 returns it: JSON:API, guid in attributes, panel related."""
    return {
        "id": str(call_id),
        "type": "calls",
        "attributes": {
            "guid": guid,
            "call_type": "mobile",
            "notification_type": "visitor",
            "logged_at": "2026-08-04T12:00:00Z",
            "status": status,
            "display_status": status.title(),
        },
        "relationships": {"panel": {"data": {"id": str(panel), "type": "panels"}}},
    }


# --- Parsing -----------------------------------------------------------------


def test_a_v3_call_yields_what_the_panel_is_addressed_by() -> None:
    """The guid and the panel are the two things v4 does not carry."""
    handle = CallHandle.from_v3(v3_call())

    assert handle == CallHandle(
        call_id=CALL_ID, guid=GUID, panel_id=PANEL_ID, status="initializing"
    )
    assert handle.is_live


@pytest.mark.parametrize(
    ("status", "live"),
    [
        ("initializing", True),
        ("canceled", False),
        ("opened_door", False),
        ("timeout_online_signal", False),
    ],
)
def test_liveness_comes_from_the_status_v3_reports_now(status: str, live: bool) -> None:
    """The only status worth trusting is the one read at the moment of asking.

    The polled v4 record cannot answer this. The coordinator skips calls it has
    already seen, so its copy keeps the status from first sight, and a ringing
    call first appears as "initializing" and stays that way there forever.
    """
    handle = CallHandle.from_v3(v3_call(status=status))

    assert handle is not None and handle.is_live is live


@pytest.mark.parametrize(
    "mangle",
    [
        lambda c: c.pop("relationships"),
        lambda c: c["attributes"].pop("guid"),
        lambda c: c.pop("id"),
    ],
    ids=["no-panel", "no-guid", "no-id"],
)
def test_an_unusable_v3_call_is_dropped(mangle) -> None:
    """Half a handle cannot address a panel, so it is not worth returning."""
    payload = v3_call()
    mangle(payload)

    assert CallHandle.from_v3(payload) is None


# --- Opening the door during a call ------------------------------------------


async def test_opening_the_door_tells_the_panel(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """The whole point: the visitor is inside, so stop dialling them.

    Pins the v3 request exactly. A wrong path or body shape fails silently, as a
    panel that keeps ringing, which no amount of v4 testing would catch.
    """
    freezer.move_to("2026-08-04T12:00:10Z")
    mock = register_topology(aioclient_mock, calls=[call_payload(call_id=CALL_ID)])
    mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    mock.get(V3_CALLS, json={"data": [v3_call()]})
    mock.post(f"{API_URL}/v3/notifications/open_door", status=204)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    sent = [
        c for c in mock.mock_calls
        if str(c[1]).endswith("/v3/notifications/open_door")
    ]
    assert len(sent) == 1, "the panel was not told the call was handled"
    assert sent[0][2] == {
        "data": {
            "type": "notifications",
            "attributes": {
                "call_guid": GUID,
                "source_id": PANEL_ID,
                "video": False,
                "audio": False,
            },
        }
    }


async def test_opening_the_door_with_no_call_says_nothing(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Most door openings have no visitor attached, and must stay one request."""
    mock = register_topology(aioclient_mock)
    mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    assert not [c for c in mock.mock_calls if "/v3/" in str(c[1])]


async def test_a_call_that_has_already_ended_is_not_notified(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """The real guard: v3 says the call is over, so there is nothing to tell.

    Recent enough to be asked about, but finished by the time we ask.
    """
    freezer.move_to("2026-08-04T12:00:10Z")
    mock = register_topology(aioclient_mock, calls=[call_payload(call_id=CALL_ID)])
    mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    mock.get(V3_CALLS, json={"data": [v3_call(status="canceled")]})
    mock.post(f"{API_URL}/v3/notifications/open_door", status=204)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    assert not [
        c for c in mock.mock_calls if str(c[1]).endswith("/notifications/open_door")
    ]


async def test_an_old_call_is_not_even_asked_about(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """The cheap filter: no visitor for hours, so do not go asking v3.

    Not a correctness guard, since the status check above is that. This only
    keeps an ordinary door opening down to a single request.
    """
    freezer.move_to("2026-08-04T12:00:10Z")
    mock = register_topology(aioclient_mock, calls=[call_payload(call_id=CALL_ID)])
    mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Well past the 40 seconds the panel rings for.
    freezer.move_to("2026-08-04T12:05:00Z")
    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    assert not [c for c in mock.mock_calls if "/v3/" in str(c[1])]


async def test_a_panel_that_cannot_be_told_does_not_fail_the_unlock(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """The door is already open by then, which is what was asked for."""
    freezer.move_to("2026-08-04T12:00:10Z")
    mock = register_topology(aioclient_mock, calls=[call_payload(call_id=CALL_ID)])
    mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    mock.get(V3_CALLS, status=500)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LOCK_DOMAIN, SERVICE_OPEN, {ATTR_ENTITY_ID: LOCK_ENTITY}, blocking=True
    )

    assert hass.states.get(LOCK_ENTITY).state == LockState.UNLOCKED


# --- Declining ----------------------------------------------------------------


async def test_declining_ends_the_call(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """Stops the dialling without opening anything."""
    freezer.move_to("2026-08-04T12:00:10Z")
    mock = register_topology(aioclient_mock, calls=[call_payload(call_id=CALL_ID)])
    mock.get(V3_CALLS, json={"data": [v3_call()]})
    mock.post(f"{API_URL}/v3/notifications/call_ended", status=204)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, SERVICE_DECLINE_CALL, {ATTR_ENTITY_ID: DOORBELL_ENTITY}, blocking=True
    )

    sent = [
        c for c in mock.mock_calls
        if str(c[1]).endswith("/v3/notifications/call_ended")
    ]
    assert len(sent) == 1
    assert sent[0][2]["data"]["attributes"]["call_guid"] == GUID
    # Declining must not open the door.
    assert not [
        c for c in mock.mock_calls if str(c[1]).endswith("/door_release_requests")
    ]


async def test_declining_nothing_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Better to say so than to silently address a call that ended."""
    register_topology(aioclient_mock)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DECLINE_CALL,
            {ATTR_ENTITY_ID: DOORBELL_ENTITY},
            blocking=True,
        )


async def test_declining_on_the_wrong_event_entity_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """The door-opened event is on the same platform and receives the call too."""
    register_topology(aioclient_mock)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_RELOCK_DELAY: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DECLINE_CALL,
            {ATTR_ENTITY_ID: "event.unit_4b_door_opened"},
            blocking=True,
        )
