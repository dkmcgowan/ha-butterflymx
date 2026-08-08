"""OAuth2 token handling for ButterflyMX.

Authorization uses PKCE, and only PKCE.  ButterflyMX issues public clients,
which have no secret to authenticate with, so the code challenge is what proves
the code came back to whoever asked for it.  Their documentation also describes
a confidential client with a secret; this deliberately does not support one.  It
would have to travel in the browser's address bar to reach the authorization
endpoint, which puts it in history, and it buys nothing the challenge does not
already provide.  Verified against a live account: authorize, exchange and
refresh all succeed with a client ID alone.

Access tokens are valid for 24 hours.  A refresh returns a *new* refresh token
along with them, confirmed against a live account, so both values have to be
persisted every time or the next refresh fails.  A 401 from the token endpoint
means the grant is gone for good and the user has to link their account again.
"""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable
import hashlib
import logging
import secrets
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


def new_code_verifier() -> str:
    """Return a fresh PKCE code verifier."""
    return secrets.token_urlsafe(64)


def code_challenge_for(verifier: str) -> str:
    """Return the S256 challenge for a PKCE verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    accounts_url: str,
    client_id: str,
    code_verifier: str,
    redirect_uri: str = OOB_REDIRECT_URI,
) -> str:
    """Build the URL the user visits to authorize Home Assistant.

    PKCE, always.  ButterflyMX issues public clients -- their own app authorizes
    this way against the same endpoint -- and a public client has no secret to
    prove itself with, so the challenge is what ties the code to us.

    ButterflyMX's own documentation puts a ``client_secret`` in this URL, which
    is what a confidential client would need.  This does not support that, and
    deliberately: it would mean a secret in the browser's address bar and in its
    history, to buy nothing that the challenge does not already provide.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge_for(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{accounts_url.rstrip('/')}{OAUTH2_AUTHORIZE_PATH}?{urlencode(params)}"


def normalize_token(raw: dict[str, Any]) -> dict[str, Any]:
    """Add an absolute ``expires_at`` to a raw token response.

    ButterflyMX documents ``expires_in`` as 86400.  If it is missing or is not a
    number the token is treated as already expired, which forces a refresh on
    first use rather than sending a request that is likely to 401.  That is the
    safe outcome, but it is unexpected enough to say so in the log.
    """
    token = dict(raw)
    expires_in = token.get("expires_in")
    if expires_in is None:
        _LOGGER.warning(
            "ButterflyMX token response had no expires_in; treating the access "
            "token as expired and refreshing before the next request"
        )
        lifetime = 0.0
    else:
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "ButterflyMX returned expires_in=%r, which is not a number; "
                "treating the access token as expired",
                expires_in,
            )
            lifetime = 0.0
    token["expires_at"] = time.time() + lifetime
    return token


async def async_exchange_code(
    session: ClientSession,
    accounts_url: str,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = OOB_REDIRECT_URI,
) -> dict[str, Any]:
    """Exchange an authorization code for a token pair.

    The verifier is what proves this is the same client that started the
    authorization; there is no secret involved at any point.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    return await _async_token_request(session, accounts_url, data)


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
        token: dict[str, Any],
        token_updater: TokenUpdater | None = None,
    ) -> None:
        """Initialize the token holder."""
        self._session = session
        self._accounts_url = accounts_url
        self._client_id = client_id
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

    async def async_force_refresh(self, stale_token: str | None = None) -> str:
        """Refresh after an unexpected 401.

        Pass the access token that got rejected.  Concurrent requests share one
        token, so a burst of 401s would otherwise each trigger a full refresh
        and rotate the refresh token several times over.  If the token already
        changed while this call waited for the lock, somebody else has fixed it
        and the replacement is returned as-is.
        """
        async with self._lock:
            if stale_token is not None and self._token.get("access_token") != stale_token:
                _LOGGER.debug("Access token already refreshed by another request")
                return str(self._token["access_token"])
            await self._async_refresh()
            return str(self._token["access_token"])

    async def _async_refresh(self) -> None:
        """Swap the refresh token for a new token pair."""
        refresh_token = self._token.get("refresh_token")
        if not refresh_token:
            raise ButterflyMXAuthError("No refresh token stored; re-authorization required")

        _LOGGER.debug("Refreshing ButterflyMX access token")
        # Client ID and refresh token, nothing else.  Verified against the live
        # authorization server, which is also what ButterflyMX documents for
        # this grant.
        new_token = await _async_token_request(
            self._session,
            self._accounts_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "client_id": self._client_id,
            },
        )

        # A refresh rotates the refresh token; keep the old one only if the
        # server did not send a replacement.
        if not new_token.get("refresh_token"):
            new_token["refresh_token"] = refresh_token

        self._token = new_token
        if self._token_updater is not None:
            await self._token_updater(dict(new_token))
