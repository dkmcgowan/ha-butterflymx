"""Tests for the throttled ButterflyMX API client."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.butterflymx.api import ButterflyMXClient
from custom_components.butterflymx.auth import ButterflyMXAuth
from custom_components.butterflymx.exceptions import (
    ButterflyMXAuthError,
    ButterflyMXRateLimitError,
    ButterflyMXResponseError,
)

from .conftest import (
    ACCESS_POINT_ID,
    ACCOUNTS_URL,
    API_URL,
    BUILDING_ID,
    DEVICES_RESPONSE,
    TENANT_ID,
    TENANTS_RESPONSE,
    call_payload,
    make_token,
)


def _client(hass: HomeAssistant, token: dict | None = None) -> ButterflyMXClient:
    session = async_get_clientsession(hass)
    auth = ButterflyMXAuth(
        session, ACCOUNTS_URL, "cid", "secret", token or make_token()
    )
    return ButterflyMXClient(session, API_URL, auth)


async def test_get_tenants_sends_self_scope(hass: HomeAssistant, aioclient_mock) -> None:
    """Only the authorized user's own tenancies are requested."""
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)
    tenants = await _client(hass).async_get_tenants()

    assert len(tenants) == 1
    assert tenants[0].id == TENANT_ID
    url = aioclient_mock.mock_calls[0][1]
    assert url.query["scope"] == "self"


async def test_pagination_follows_next_page(hass: HomeAssistant, aioclient_mock) -> None:
    """Paginated list endpoints are drained."""
    aioclient_mock.get(
        f"{API_URL}/v4/devices?page=1",
        json={
            "data": DEVICES_RESPONSE["data"][:1],
            "page_info": {"next_page": 2},
        },
    )
    aioclient_mock.get(
        f"{API_URL}/v4/devices?page=2",
        json={
            "data": DEVICES_RESPONSE["data"][1:],
            "page_info": {"next_page": None},
        },
    )

    devices = await _client(hass).async_get_devices(BUILDING_ID)
    assert [device.id for device in devices] == [5005, 2002]


async def test_authorization_header_is_sent(hass: HomeAssistant, aioclient_mock) -> None:
    """Every API call carries the bearer token."""
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)
    await _client(hass).async_get_tenants()

    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == "Bearer access-1"


async def test_retries_on_rate_limit(hass: HomeAssistant, aioclient_mock) -> None:
    """A 429 with Retry-After is retried rather than surfaced."""
    attempts: list[int] = []

    async def _side_effect(method, url, data):
        attempts.append(1)
        if len(attempts) == 1:
            return AiohttpClientMockResponse(
                method, url, status=429, headers={"Retry-After": "0"}
            )
        return AiohttpClientMockResponse(method, url, status=200, json=TENANTS_RESPONSE)

    aioclient_mock.get(f"{API_URL}/v4/tenants", side_effect=_side_effect)
    tenants = await _client(hass).async_get_tenants()

    assert len(attempts) == 2
    assert len(tenants) == 1


async def test_rate_limit_eventually_raises(hass: HomeAssistant, aioclient_mock) -> None:
    """Persistent 429s surface once retries are exhausted."""
    aioclient_mock.get(
        f"{API_URL}/v4/tenants", status=429, headers={"Retry-After": "0"}, json={}
    )

    with pytest.raises(ButterflyMXRateLimitError):
        await _client(hass).async_get_tenants()


async def test_401_triggers_one_refresh(hass: HomeAssistant, aioclient_mock) -> None:
    """An unexpected 401 forces a refresh and retries once."""
    aioclient_mock.post(
        f"{ACCOUNTS_URL}/oauth/token",
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 86400},
    )
    attempts: list[int] = []

    async def _side_effect(method, url, data):
        attempts.append(1)
        if len(attempts) == 1:
            return AiohttpClientMockResponse(method, url, status=401, json={})
        return AiohttpClientMockResponse(method, url, status=200, json=TENANTS_RESPONSE)

    aioclient_mock.get(f"{API_URL}/v4/tenants", side_effect=_side_effect)
    tenants = await _client(hass).async_get_tenants()

    assert len(attempts) == 2
    assert len(tenants) == 1


async def test_repeated_401_raises_auth_error(hass: HomeAssistant, aioclient_mock) -> None:
    """A token that stays rejected asks for re-authorization."""
    aioclient_mock.post(
        f"{ACCOUNTS_URL}/oauth/token",
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 86400},
    )
    aioclient_mock.get(f"{API_URL}/v4/tenants", status=401, json={})

    with pytest.raises(ButterflyMXAuthError):
        await _client(hass).async_get_tenants()


async def test_client_error_is_raised(hass: HomeAssistant, aioclient_mock) -> None:
    """A 4xx that is not retryable is reported with its status."""
    aioclient_mock.get(f"{API_URL}/v4/tenants", status=403, json={})

    with pytest.raises(ButterflyMXResponseError) as err:
        await _client(hass).async_get_tenants()
    assert err.value.status == 403


async def test_release_door_by_access_point(hass: HomeAssistant, aioclient_mock) -> None:
    """A door release names the tenant and the access point."""
    aioclient_mock.post(
        f"{API_URL}/v4/door_release_requests",
        status=201,
        json={"data": {"id": 1, "release_method": "app"}},
    )

    result = await _client(hass).async_release_door(
        TENANT_ID, access_point_id=ACCESS_POINT_ID
    )

    assert result["id"] == 1
    body = aioclient_mock.mock_calls[0][2]
    assert body == {
        "door_release_request": {
            "tenant_id": TENANT_ID,
            "access_point_id": ACCESS_POINT_ID,
        }
    }


async def test_release_door_by_device(hass: HomeAssistant, aioclient_mock) -> None:
    """A unit smart lock is released by device ID."""
    aioclient_mock.post(f"{API_URL}/v4/door_release_requests", status=201, json={})
    await _client(hass).async_release_door(TENANT_ID, device_id=2002)

    body = aioclient_mock.mock_calls[0][2]
    assert body["door_release_request"]["device_id"] == 2002


@pytest.mark.parametrize(
    ("access_point_id", "device_id"),
    [(None, None), (1, 2)],
)
async def test_release_door_requires_exactly_one_target(
    hass: HomeAssistant, access_point_id: int | None, device_id: int | None
) -> None:
    """The API requires exactly one of access point or device."""
    with pytest.raises(ValueError):
        await _client(hass).async_release_door(
            TENANT_ID, access_point_id=access_point_id, device_id=device_id
        )


async def test_release_door_is_never_retried(hass: HomeAssistant, aioclient_mock) -> None:
    """A failed release is reported, not repeated - it is not idempotent."""
    attempts: list[int] = []

    async def _side_effect(method, url, data):
        attempts.append(1)
        return AiohttpClientMockResponse(method, url, status=503, json={})

    aioclient_mock.post(f"{API_URL}/v4/door_release_requests", side_effect=_side_effect)

    with pytest.raises(ButterflyMXResponseError):
        await _client(hass).async_release_door(TENANT_ID, access_point_id=ACCESS_POINT_ID)
    assert len(attempts) == 1


async def test_get_calls_filters_by_timestamp(hass: HomeAssistant, aioclient_mock) -> None:
    """The call poll asks only for calls newer than the last one seen."""
    from homeassistant.util import dt as dt_util

    aioclient_mock.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/calls",
        json={"data": [call_payload()], "page_info": {"next_page": None}},
    )
    since = dt_util.parse_datetime("2026-08-04T11:00:00Z")
    calls = await _client(hass).async_get_calls(BUILDING_ID, since=since)

    assert len(calls) == 1
    url = aioclient_mock.mock_calls[0][1]
    assert url.query["q[logged_at_gteq]"] == "2026-08-04 11:00:00 UTC"


async def test_fetch_image_without_auth(hass: HomeAssistant, aioclient_mock) -> None:
    """Pre-signed snapshot URLs are fetched without credentials."""
    aioclient_mock.get("https://cdn.example.com/snap.png", content=b"\x89PNG\r\n\x1a\nrest")
    data = await _client(hass).async_fetch_image("https://cdn.example.com/snap.png")

    assert data is not None and data.startswith(b"\x89PNG")


async def test_fetch_image_failure_returns_none(hass: HomeAssistant, aioclient_mock) -> None:
    """A broken snapshot URL does not take the entity down."""
    aioclient_mock.get("https://cdn.example.com/snap.png", status=404, content=b"")
    assert await _client(hass).async_fetch_image("https://cdn.example.com/snap.png") is None
