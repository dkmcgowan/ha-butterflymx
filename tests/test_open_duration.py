"""Tests for reading how long each door stays open.

ButterflyMX configures this per access point and only reports it over GraphQL.
Getting it wrong is not cosmetic: on a real building the three doors on one
panel were set to 4, 9 and 14 seconds, so one fixed number is wrong for at least
two of them.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.butterflymx.api import ButterflyMXClient
from custom_components.butterflymx.auth import ButterflyMXAuth
from custom_components.butterflymx.const import FALLBACK_OPEN_SECONDS
from custom_components.butterflymx.exceptions import ButterflyMXResponseError
from custom_components.butterflymx.graphql import (
    ACCESS_POINT_DETAIL_QUERY,
    parse_access_point_details,
)
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


# --- Reading a result ---------------------------------------------------------


def test_details_are_keyed_by_access_point() -> None:
    """The query result comes back ready to look up by access point ID."""
    details = parse_access_point_details(graphql_access_points())

    assert set(details) == {ACCESS_POINT_ID}
    assert details[ACCESS_POINT_ID].open_duration == ACCESS_POINT_OPEN_DURATION


def test_graphql_reports_failure_inside_a_200() -> None:
    """An errors array is a failed query, not an account with no doors."""
    with pytest.raises(ButterflyMXResponseError, match="legacyId"):
        parse_access_point_details(
            {"errors": [{"message": "Field 'legacyId' doesn't exist"}]}
        )


def test_an_account_with_no_doors_is_not_an_error() -> None:
    """Empty is a real answer, and the caller already handles a missing value."""
    assert parse_access_point_details({"data": {"tenants": None}}) == {}


def test_a_second_page_is_reported_rather_than_dropped(caplog) -> None:
    """Doors we did not read must not look like doors that do not exist."""
    details = parse_access_point_details(graphql_access_points(has_next_page=True))

    assert set(details) == {ACCESS_POINT_ID}
    assert "more access points than one page" in caplog.text


# --- The request ---------------------------------------------------------------


async def test_the_query_is_posted_to_the_graphql_endpoint(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The one thing the client half owns is where the query goes."""
    aioclient_mock.post(GRAPHQL_URL, json=graphql_access_points())

    details = await _client(hass).async_get_access_point_details()

    assert set(details) == {ACCESS_POINT_ID}
    method, url, body, _ = aioclient_mock.mock_calls[0]
    assert method.lower() == "post"
    assert url.path == "/denizen/v1/graphql"
    assert body["query"] == ACCESS_POINT_DETAIL_QUERY


# --- The lock entity ----------------------------------------------------------


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_door_reports_its_own_duration(
    hass: HomeAssistant,
    aioclient_mock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A door stays unlocked for as long as ButterflyMX says, not a fixed guess."""
    register_topology(aioclient_mock)
    aioclient_mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    await _setup(hass, config_entry)

    state = hass.states.get(LOCK_ENTITY)
    assert state.attributes["open_duration"] == ACCESS_POINT_OPEN_DURATION
    assert state.attributes["online"] is True

    await hass.services.async_call(
        "lock", "open", {"entity_id": LOCK_ENTITY}, blocking=True
    )
    assert hass.states.get(LOCK_ENTITY).state == "unlocked"

    # Past the fallback, nowhere near this door's twelve seconds.  Getting this
    # wrong is the whole reason the duration is read at all.
    freezer.tick(timedelta(seconds=FALLBACK_OPEN_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK_ENTITY).state == "unlocked"

    freezer.tick(timedelta(seconds=ACCESS_POINT_OPEN_DURATION))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK_ENTITY).state == "locked"


async def test_a_door_with_no_duration_uses_the_fallback(
    hass: HomeAssistant,
    aioclient_mock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed GraphQL read leaves the door working on a fixed guess.

    There is no setting to fall back to, so this is the only path for a door
    that is not an access point or a refresh where the query failed.
    """
    register_topology(
        aioclient_mock,
        access_point_details={"errors": [{"message": "nope"}]},
    )
    aioclient_mock.post(
        f"{API_URL}/v4/door_release_requests", status=201, json={"data": {"id": 1}}
    )
    await _setup(hass, config_entry)

    state = hass.states.get(LOCK_ENTITY)
    assert state.state == "locked"
    assert state.attributes["open_duration"] == FALLBACK_OPEN_SECONDS
    assert state.attributes["online"] is None

    await hass.services.async_call(
        "lock", "open", {"entity_id": LOCK_ENTITY}, blocking=True
    )
    assert hass.states.get(LOCK_ENTITY).state == "unlocked"

    freezer.tick(timedelta(seconds=FALLBACK_OPEN_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK_ENTITY).state == "locked"
