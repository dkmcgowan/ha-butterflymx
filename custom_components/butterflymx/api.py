"""Async client for the ButterflyMX v4 REST API.

ButterflyMX publishes no rate limits, so this client stays well inside any
plausible one: it caps concurrency, spaces requests out, retries only idempotent
calls, and honors ``Retry-After`` when the server pushes back.  Door releases
are never retried, because firing a door twice after a slow response is worse
than failing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import logging
import random
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .auth import ButterflyMXAuth
from .const import (
    API_VERSION_PATH,
    BACKOFF_BASE,
    BACKOFF_MAX,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
    PAGE_SIZE,
    REQUEST_TIMEOUT,
    WEBHOOK_RESOURCE_CALL,
)
from .exceptions import (
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
    ButterflyMXRateLimitError,
    ButterflyMXResponseError,
)
from .models import AccessPoint, Call, Device, Tenant

_LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class _Throttler:
    """Cap concurrency and enforce a minimum gap between requests."""

    def __init__(self, max_concurrent: int, min_interval: float) -> None:
        """Initialize the throttler."""
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._spacing_lock = asyncio.Lock()
        self._min_interval = min_interval
        self._next_slot = 0.0

    async def acquire(self) -> None:
        """Wait until it is polite to send the next request."""
        await self._semaphore.acquire()
        try:
            async with self._spacing_lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                wait = self._next_slot - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = loop.time()
                self._next_slot = now + self._min_interval
        except BaseException:
            # The slot is only handed back by __aexit__, which never runs if
            # __aenter__ raises.  Cancellation while waiting here would
            # otherwise retire a slot permanently, and losing all of them
            # deadlocks every later request.
            self._semaphore.release()
            raise

    def release(self) -> None:
        """Release the concurrency slot."""
        self._semaphore.release()

    async def __aenter__(self) -> None:
        """Enter the throttle."""
        await self.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the throttle."""
        self.release()


def _retry_after_seconds(response: ClientResponse) -> float | None:
    """Read a ``Retry-After`` header, if the server sent a usable one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The header may be an HTTP-date; fall back to the caller's backoff.
        return None


class ButterflyMXClient:
    """Thin, throttled wrapper around the ButterflyMX v4 API."""

    def __init__(
        self,
        session: ClientSession,
        api_url: str,
        auth: ButterflyMXAuth,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._api_url = api_url.rstrip("/")
        self._auth = auth
        self._throttle = _Throttler(MAX_CONCURRENT_REQUESTS, MIN_REQUEST_INTERVAL)

    @property
    def auth(self) -> ButterflyMXAuth:
        """Return the auth holder backing this client."""
        return self._auth

    # -- Low level ------------------------------------------------------------

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform an authenticated request and return the decoded body."""
        url = f"{self._api_url}{API_VERSION_PATH}{path}"
        attempt = 0
        refreshed = False

        while True:
            attempt += 1
            token = await self._auth.async_get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }

            try:
                async with self._throttle:
                    response = await self._session.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        headers=headers,
                        timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                    )
                    body = await response.read()
            except TimeoutError as err:
                if retry and attempt <= MAX_RETRIES:
                    await self._async_backoff(attempt)
                    continue
                raise ButterflyMXConnectionError(f"Timeout calling {method} {path}") from err
            except ClientError as err:
                if retry and attempt <= MAX_RETRIES:
                    await self._async_backoff(attempt)
                    continue
                raise ButterflyMXConnectionError(f"Error calling {method} {path}: {err}") from err

            if response.status == 401:
                # The token may have been revoked early; try exactly one forced
                # refresh before giving up and asking the user to re-link.
                if not refreshed:
                    refreshed = True
                    _LOGGER.debug("Got 401 on %s %s; forcing a token refresh", method, path)
                    # Hand back the token that was rejected so parallel requests
                    # hitting the same 401 do not each rotate the token pair.
                    await self._auth.async_force_refresh(token)
                    continue
                raise ButterflyMXAuthError(
                    f"ButterflyMX rejected the access token for {method} {path}"
                )

            if response.status in RETRYABLE_STATUSES:
                retry_after = _retry_after_seconds(response)
                if retry and attempt <= MAX_RETRIES:
                    await self._async_backoff(attempt, retry_after)
                    continue
                if response.status == 429:
                    raise ButterflyMXRateLimitError(
                        f"ButterflyMX rate limited {method} {path}", retry_after
                    )
                raise ButterflyMXResponseError(
                    f"ButterflyMX returned HTTP {response.status} for {method} {path}",
                    response.status,
                )

            if response.status >= 400:
                raise ButterflyMXResponseError(
                    f"ButterflyMX returned HTTP {response.status} for {method} {path}: "
                    f"{body[:300]!r}",
                    response.status,
                )

            if not body:
                return None
            try:
                return await response.json(content_type=None)
            except ValueError as err:
                raise ButterflyMXResponseError(
                    f"ButterflyMX returned a non-JSON body for {method} {path}"
                ) from err

    async def _async_backoff(self, attempt: int, retry_after: float | None = None) -> None:
        """Sleep before retrying, honoring the server's Retry-After."""
        if retry_after is not None:
            delay = min(retry_after, BACKOFF_MAX)
        else:
            delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
            delay += random.uniform(0, delay * 0.25)
        _LOGGER.debug("Backing off %.1fs before retry %s", delay, attempt)
        await asyncio.sleep(delay)

    async def _async_get_paginated(
        self, path: str, params: Mapping[str, Any] | None = None, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        """Follow ``page_info`` until the API runs out of pages."""
        collected: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            query: dict[str, Any] = dict(params or {})
            query["page"] = page
            query["per"] = PAGE_SIZE
            payload = await self._async_request("GET", path, params=query)
            if not isinstance(payload, dict):
                break
            data = payload.get("data")
            if isinstance(data, list):
                collected.extend(item for item in data if isinstance(item, dict))
            page_info = payload.get("page_info") or {}
            next_page = page_info.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        return collected

    # -- Topology -------------------------------------------------------------

    async def async_get_tenants(self, own_only: bool = True) -> list[Tenant]:
        """List the tenant records the authorized user can act as."""
        params = {"scope": "self"} if own_only else None
        raw = await self._async_get_paginated("/tenants", params)
        tenants = [Tenant.from_api(item) for item in raw]
        return [tenant for tenant in tenants if tenant is not None]

    async def async_get_access_points(self, building_id: int) -> list[AccessPoint]:
        """List access points in a building."""
        raw = await self._async_get_paginated(
            "/access_points", {"q[building_id_eq]": str(building_id)}
        )
        points = [AccessPoint.from_api(item) for item in raw]
        return [point for point in points if point is not None]

    async def async_get_devices(self, building_id: int) -> list[Device]:
        """List devices in a building."""
        raw = await self._async_get_paginated(
            "/devices", {"q[building_id_eq]": str(building_id)}
        )
        devices = [Device.from_api(item) for item in raw]
        return [device for device in devices if device is not None]

    # -- Calls ----------------------------------------------------------------

    async def async_get_calls(
        self, building_id: int, since: datetime | None = None, limit: int = 20
    ) -> list[Call]:
        """List recent calls for a building, newest first.

        Only the first page is fetched, since this runs on the fast poll loop and
        the integration only cares about calls it has not seen yet.
        """
        params: dict[str, Any] = {"page": 1, "per": min(limit, PAGE_SIZE)}
        if since is not None:
            params["q[logged_at_gteq]"] = since.strftime("%Y-%m-%d %H:%M:%S UTC")
        payload = await self._async_request(
            "GET", f"/buildings/{building_id}/calls", params=params
        )
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        calls = [
            Call.from_api(item, building_id) for item in data if isinstance(item, dict)
        ]
        return [call for call in calls if call is not None]

    # -- Actions --------------------------------------------------------------

    async def async_release_door(
        self,
        tenant_id: int,
        *,
        access_point_id: int | None = None,
        device_id: int | None = None,
    ) -> dict[str, Any]:
        """Open a door.

        Exactly one of ``access_point_id`` or ``device_id`` must be supplied.
        This request is not idempotent, so it is sent with retries disabled.
        """
        if (access_point_id is None) == (device_id is None):
            raise ValueError("Provide exactly one of access_point_id or device_id")

        body: dict[str, Any] = {"door_release_request": {"tenant_id": tenant_id}}
        if access_point_id is not None:
            body["door_release_request"]["access_point_id"] = access_point_id
        else:
            body["door_release_request"]["device_id"] = device_id

        payload = await self._async_request(
            "POST", "/door_release_requests", json=body, retry=False
        )
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return data
        return {}

    # -- Images ---------------------------------------------------------------

    async def async_fetch_image(self, url: str) -> bytes | None:
        """Download a call snapshot.

        Snapshot URLs are usually pre-signed and need no credentials, but retry
        once with a bearer token in case the deployment serves them from the API.
        """
        for use_auth in (False, True):
            headers: dict[str, str] = {}
            if use_auth:
                headers["Authorization"] = f"Bearer {await self._auth.async_get_access_token()}"
            try:
                async with self._throttle:
                    response = await self._session.get(
                        url, headers=headers, timeout=ClientTimeout(total=REQUEST_TIMEOUT)
                    )
                    if response.status in (401, 403) and not use_auth:
                        continue
                    if response.status >= 400:
                        _LOGGER.debug(
                            "Snapshot download failed with HTTP %s", response.status
                        )
                        return None
                    return await response.read()
            except (TimeoutError, ClientError) as err:
                _LOGGER.debug("Snapshot download failed: %s", err)
                return None
        return None

    # -- Webhook integrations -------------------------------------------------

    async def async_list_tenant_integrations(self, tenant_id: int) -> list[dict[str, Any]]:
        """List webhook integrations registered for a tenant."""
        payload = await self._async_request("GET", f"/tenants/{tenant_id}/integrations")
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    async def async_create_tenant_integration(
        self, tenant_id: int, webhook_url: str, resources: list[str] | None = None
    ) -> dict[str, Any]:
        """Register a webhook integration for a tenant."""
        bindings = [
            {"actions": ["create"], "resource_type": resource, "resource_id": None}
            for resource in (resources or [WEBHOOK_RESOURCE_CALL])
        ]
        body = {
            "data": {
                "type": "integrations",
                "attributes": {
                    "integrator": "webhook",
                    "configuration": {"url": webhook_url, "method": "post"},
                    "bindings": bindings,
                },
            }
        }
        payload = await self._async_request(
            "POST", f"/tenants/{tenant_id}/integrations", json=body, retry=False
        )
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return data
        return {}

    async def async_delete_tenant_integration(
        self, tenant_id: int, integration_id: str
    ) -> None:
        """Remove a webhook integration from a tenant."""
        await self._async_request(
            "DELETE", f"/tenants/{tenant_id}/integrations/{integration_id}", retry=False
        )
