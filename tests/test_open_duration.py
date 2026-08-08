"""Tests for reading how long each door stays open.

ButterflyMX configures this per access point and only reports it over GraphQL.
Getting it wrong is not cosmetic: on a real building the three doors on one
panel were set to 4, 9 and 14 seconds, so a single relock delay is wrong for at
least two of them.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.api import ButterflyMXClient
from custom_components.butterflymx.auth import ButterflyMXAuth
from custom_components.butterflymx.exceptions import ButterflyMXResponseError
from custom_components.butterflymx.models import AccessPointDetail

from .conftest import (
    ACCESS_POINT_ID,
    ACCESS_POINT_OPEN_DURATION,
    ACCOUNTS_URL,
    API_URL,
    graphql_access_points,
    make_token,
    register_topology,
)

GRAPHQL_URL = f"{API_URL}/denizen/v1/graphql"
LOCK_ENTITY = "lock.front_entrance"


def _client(hass: HomeAssistant) -> ButterflyMXClient:
    session = async_get_clientsession(hass)
    auth = ButterflyMXAuth(session, ACCOUNTS_URL, "cid", make_token())
    return ButterflyMXClient(session, API_URL, auth)


# --- Parsing ------------------------------------------------------------------


def test_detail_joins_on_the_id_v4_already_uses() -> None:
    """The legacyId field is the access point ID, arriving as a string."""
    detail = AccessPointDetail.from_graphql(
        {
            "legacyId": "60776",
            "name": "Front and Inner Door",
            "openDuration": 9,
            "online": True,
            "inOpenHours": False,
        }
    )

    assert detail is not None
    assert detail.access_point_id == 60776
    assert detail.open_duration == 9
    assert detail.online is True


@pytest.mark.parametrize("duration", [0, None, "", "soon"])
def test_a_duration_that_is_not_a_positive_number_is_no_duration(duration) -> None:
    """Zero would mean a door that never opens, which must not reach a timer."""
    detail = AccessPointDetail.from_graphql(
        {"legacyId": "1", "openDuration": duration}
    )

    assert detail is not None
    assert detail.open_duration is None


def test_a_node_with_nothing_to_join_on_is_dropped() -> None:
    """Without legacyId there is no door to attach the duration to."""
    assert AccessPointDetail.from_graphql({"name": "Front Door"}) is None


# --- The client ---------------------------------------------------------------


async def test_details_are_keyed_by_access_point(hass: HomeAssistant, aioclient_mock):
    """The query result comes back ready to look up by access point ID."""
    aioclient_mock.post(GRAPHQL_URL, json=graphql_access_points())

    details = await _client(hass).async_get_access_point_details()

    assert set(details) == {ACCESS_POINT_ID}
    assert details[ACCESS_POINT_ID].open_duration == ACCESS_POINT_OPEN_DURATION


async def test_graphql_reports_failure_inside_a_200(
    hass: HomeAssistant, aioclient_mock
):
    """A 200 with an errors array is a failed query, not an empty account."""
    aioclient_mock.post(
        GRAPHQL_URL,
        json={"errors": [{"message": "Field 'legacyId' doesn't exist"}]},
    )

    with pytest.raises(ButterflyMXResponseError, match="legacyId"):
        await _client(hass).async_get_access_point_details()


async def test_a_second_page_is_reported_rather_than_dropped(
    hass: HomeAssistant, aioclient_mock, caplog
):
    """Doors we did not read must not look like doors that do not exist."""
    aioclient_mock.post(GRAPHQL_URL, json=graphql_access_points(has_next_page=True))

    details = await _client(hass).async_get_access_point_details()

    assert set(details) == {ACCESS_POINT_ID}
    assert "more access points than one page" in caplog.text


# --- The lock entity ----------------------------------------------------------


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, relock_delay: int
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "relock_delay": relock_delay}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_door_beats_the_option(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A door that reports its own duration ignores the configured delay.

    The relock timer reads the same ``_open_seconds`` this attribute exposes,
    so asserting the attribute asserts the timer.  It is not asserted by
    waiting: the timer is a real ``asyncio.sleep``, which a frozen clock does
    not move, so a timing test here would sleep for real.
    """
    register_topology(aioclient_mock)
    await _setup(hass, config_entry, relock_delay=1)

    state = hass.states.get(LOCK_ENTITY)
    assert state is not None
    assert state.attributes["open_duration"] == ACCESS_POINT_OPEN_DURATION
    assert state.attributes["online"] is True


async def test_lock_falls_back_when_the_duration_cannot_be_read(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A failed GraphQL read leaves the door usable on its configured delay."""
    register_topology(
        aioclient_mock,
        access_point_details={"errors": [{"message": "nope"}]},
    )
    await _setup(hass, config_entry, relock_delay=7)

    state = hass.states.get(LOCK_ENTITY)
    assert state is not None
    assert state.state == "locked"
    assert state.attributes["open_duration"] == 7
    assert state.attributes["online"] is None
