"""OAuth2 token handling for ButterflyMX.

ButterflyMX issues access tokens that are valid for 24 hours and refresh tokens
that do not expire.  A refresh returns a *new* refresh token as well, so both
values have to be persisted every time.  A 401 from the token endpoint means the
grant is gone for good and the user has to link their account again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    OAUTH2_AUTHORIZE_PATH,
    OAUTH2_TOKEN_PATH,
    OOB_REDIRECT_URI,
    REQUEST_TIMEOUT,
    TOKEN_EXPIRY_MARGIN,
)
from .exceptions import ButterflyMXAuthError, ButterflyMXConnectionError

_LOGGER = logging.getLogger(__name__)

TokenUpdater = Callable[[dict[str, Any]], Awaitable[None]]


def build_authorize_url(
    accounts_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = OOB_REDIRECT_URI,
) -> str:
    """Build the URL the user visits to authorize Home Assistant.

    ButterflyMX's documented authorize URL includes ``client_secret``; their
    authorization server expects it even though that is unusual for the
    authorization-code grant.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
    )
    return f"{accounts_url.rstrip('/')}{OAUTH2_AUTHORIZE_PATH}?{query}"


def normalize_token(raw: dict[str, Any]) -> dict[str, Any]:
    """Add an absolute ``expires_at`` to a raw token response."""
    token = dict(raw)
    expires_in = token.get("expires_in")
    try:
        lifetime = float(expires_in) if expires_in is not None else 0.0
    except (TypeError, ValueError):
        lifetime = 0.0
    token["expires_at"] = time.time() + lifetime
    return token


async def async_exchange_code(
    session: ClientSession,
    accounts_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = OOB_REDIRECT_URI,
) -> dict[str, Any]:
    """Exchange an authorization code for a token pair."""
    return await _async_token_request(
        session,
        accounts_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )


async def _async_token_request(
    session: ClientSession, accounts_url: str, data: dict[str, str]
) -> dict[str, Any]:
    """POST to the token endpoint and return a normalized token."""
    url = f"{accounts_url.rstrip('/')}{OAUTH2_TOKEN_PATH}"
    try:
        response = await session.post(
            url, data=data, timeout=ClientTimeout(total=REQUEST_TIMEOUT)
        )
        body = await response.text()
    except TimeoutError as err:
        raise ButterflyMXConnectionError("Timeout talking to ButterflyMX accounts") from err
    except ClientError as err:
        raise ButterflyMXConnectionError(f"Error talking to ButterflyMX accounts: {err}") from err

    if response.status in (400, 401, 403):
        # Doorkeeper reports invalid_grant/invalid_client with a 400 or 401.
        raise ButterflyMXAuthError(
            f"ButterflyMX rejected the credentials (HTTP {response.status}): {body[:300]}"
        )
    if response.status >= 400:
        raise ButterflyMXConnectionError(
            f"Unexpected response from ButterflyMX accounts (HTTP {response.status})"
        )

    try:
        payload = await response.json(content_type=None)
    except ValueError as err:
        raise ButterflyMXConnectionError("ButterflyMX returned a non-JSON token response") from err

    if not isinstance(payload, dict) or "access_token" not in payload:
        raise ButterflyMXAuthError("ButterflyMX token response did not contain an access token")

    return normalize_token(payload)


class ButterflyMXAuth:
    """Holds a token pair and keeps it fresh."""

    def __init__(
        self,
        session: ClientSession,
        accounts_url: str,
        client_id: str,
        client_secret: str,
        token: dict[str, Any],
        token_updater: TokenUpdater | None = None,
    ) -> None:
        """Initialise the token holder."""
        self._session = session
        self._accounts_url = accounts_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = dict(token)
        self._token_updater = token_updater
        self._lock = asyncio.Lock()

    @property
    def token(self) -> dict[str, Any]:
        """Return the current token."""
        return dict(self._token)

    @property
    def valid(self) -> bool:
        """Return True when the access token is still usable."""
        expires_at = self._token.get("expires_at")
        if not expires_at:
            return False
        return float(expires_at) - TOKEN_EXPIRY_MARGIN > time.time()

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing it if needed."""
        if self.valid:
            return str(self._token["access_token"])

        async with self._lock:
            # Another task may have refreshed while we waited for the lock.
            if self.valid:
                return str(self._token["access_token"])
            await self._async_refresh()
            return str(self._token["access_token"])

    async def async_force_refresh(self) -> str:
        """Refresh unconditionally, after an unexpected 401."""
        async with self._lock:
            await self._async_refresh()
            return str(self._token["access_token"])

    async def _async_refresh(self) -> None:
        """Swap the refresh token for a new token pair."""
        refresh_token = self._token.get("refresh_token")
        if not refresh_token:
            raise ButterflyMXAuthError("No refresh token stored; re-authorization required")

        _LOGGER.debug("Refreshing ButterflyMX access token")
        new_token = await _async_token_request(
            self._session,
            self._accounts_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )

        # A refresh rotates the refresh token; keep the old one only if the
        # server did not send a replacement.
        if not new_token.get("refresh_token"):
            new_token["refresh_token"] = refresh_token

        self._token = new_token
        if self._token_updater is not None:
            await self._token_updater(dict(new_token))
