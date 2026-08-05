"""Tests for ButterflyMX OAuth token handling."""

from __future__ import annotations

import asyncio
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.butterflymx.auth import (
    ButterflyMXAuth,
    async_exchange_code,
    build_authorize_url,
    normalize_token,
)
from custom_components.butterflymx.exceptions import (
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
)

from .conftest import ACCOUNTS_URL, make_token

TOKEN_URL = f"{ACCOUNTS_URL}/oauth/token"


def test_build_authorize_url() -> None:
    """The authorize URL carries every parameter ButterflyMX documents."""
    url = build_authorize_url(ACCOUNTS_URL, "cid", "secret", "urn:ietf:wg:oauth:2.0:oob")
    assert url.startswith(f"{ACCOUNTS_URL}/oauth/authorize?")
    assert "client_id=cid" in url
    assert "client_secret=secret" in url
    assert "response_type=code" in url
    assert "redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in url


def test_normalize_token_adds_absolute_expiry() -> None:
    """expires_in is turned into an absolute deadline."""
    token = normalize_token({"access_token": "a", "expires_in": 86400})
    assert token["expires_at"] == pytest.approx(time.time() + 86400, abs=5)


@pytest.mark.parametrize(
    "raw",
    [{"access_token": "a"}, {"access_token": "a", "expires_in": "soon"}],
)
def test_normalize_token_without_usable_expiry_warns(
    raw: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing or unreadable expires_in expires the token and says so."""
    token = normalize_token(raw)
    assert token["expires_at"] <= time.time() + 1
    assert "expired" in caplog.text.lower()


async def test_exchange_code(hass: HomeAssistant, aioclient_mock) -> None:
    """An authorization code is swapped for a token pair."""
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "token_type": "Bearer",
            "expires_in": 86400,
        },
    )
    session = async_get_clientsession(hass)
    token = await async_exchange_code(session, ACCOUNTS_URL, "cid", "secret", "code")

    assert token["access_token"] == "access-1"
    assert token["expires_at"] > time.time()
    assert aioclient_mock.mock_calls[0][2]["grant_type"] == "authorization_code"


async def test_exchange_code_rejected(hass: HomeAssistant, aioclient_mock) -> None:
    """A rejected code raises an auth error, not a connection error."""
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    session = async_get_clientsession(hass)

    with pytest.raises(ButterflyMXAuthError):
        await async_exchange_code(session, ACCOUNTS_URL, "cid", "secret", "bad")


async def test_exchange_code_server_error(hass: HomeAssistant, aioclient_mock) -> None:
    """A 5xx from the accounts host is a connection problem."""
    aioclient_mock.post(TOKEN_URL, status=503, text="down")
    session = async_get_clientsession(hass)

    with pytest.raises(ButterflyMXConnectionError):
        await async_exchange_code(session, ACCOUNTS_URL, "cid", "secret", "code")


async def test_valid_token_is_not_refreshed(hass: HomeAssistant, aioclient_mock) -> None:
    """A token with plenty of life left is reused as-is."""
    auth = ButterflyMXAuth(
        async_get_clientsession(hass), ACCOUNTS_URL, "cid", "secret", make_token()
    )
    assert await auth.async_get_access_token() == "access-1"
    assert not aioclient_mock.mock_calls


async def test_expiring_token_is_refreshed_and_persisted(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Both halves of a rotated token pair are stored."""
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 86400,
        },
    )
    saved: list[dict] = []

    async def _save(token: dict) -> None:
        saved.append(token)

    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=10),
        token_updater=_save,
    )

    assert await auth.async_get_access_token() == "access-2"
    assert auth.token["refresh_token"] == "refresh-2"
    assert saved and saved[0]["refresh_token"] == "refresh-2"
    assert aioclient_mock.mock_calls[0][2]["grant_type"] == "refresh_token"


async def test_refresh_keeps_old_token_when_none_returned(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """If the server omits a new refresh token, keep the existing one."""
    aioclient_mock.post(TOKEN_URL, json={"access_token": "access-2", "expires_in": 86400})
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=10),
    )

    await auth.async_get_access_token()
    assert auth.token["refresh_token"] == "refresh-1"


async def test_revoked_refresh_token_raises_auth_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A revoked grant surfaces as an auth error so reauth can start."""
    aioclient_mock.post(TOKEN_URL, status=401, json={"error": "invalid_grant"})
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=0),
    )

    with pytest.raises(ButterflyMXAuthError):
        await auth.async_get_access_token()


async def test_refresh_grant_omits_client_secret(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The refresh grant sends exactly what ButterflyMX documents."""
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 86400},
    )
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=10),
    )

    await auth.async_get_access_token()

    body = aioclient_mock.mock_calls[0][2]
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "refresh-1"
    assert body["client_id"] == "cid"
    assert "client_secret" not in body


async def test_concurrent_forced_refresh_only_refreshes_once(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Parallel 401s share one refresh instead of rotating the pair repeatedly."""
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 86400},
    )
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=10),
    )

    results = await asyncio.gather(
        *(auth.async_force_refresh("access-1") for _ in range(4))
    )

    assert results == ["access-2"] * 4
    assert len(aioclient_mock.mock_calls) == 1


async def test_forced_refresh_runs_again_for_a_different_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A second, genuinely stale token still triggers its own refresh."""
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 86400},
    )
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        make_token(expires_in=10),
    )

    await auth.async_force_refresh("access-1")
    await auth.async_force_refresh("access-2")

    assert len(aioclient_mock.mock_calls) == 2


async def test_missing_refresh_token_raises(hass: HomeAssistant) -> None:
    """Without a refresh token there is nothing to renew."""
    auth = ButterflyMXAuth(
        async_get_clientsession(hass),
        ACCOUNTS_URL,
        "cid",
        "secret",
        {"access_token": "a", "expires_at": 0},
    )

    with pytest.raises(ButterflyMXAuthError):
        await auth.async_get_access_token()
