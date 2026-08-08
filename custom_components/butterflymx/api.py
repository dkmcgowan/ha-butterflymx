"""Async client for the ButterflyMX v4 REST API.

There is no request throttling here on purpose.  ButterflyMX publishes no rate
limits, returns no rate-limit headers, and their own app does not pace itself;
the integration also issues its requests one after another rather than in
parallel, so a client-side cap had nothing to cap.  What it did do was add a
fixed delay to every call and, for a while, deadlock the client when a request
was cancelled mid-wait.

What is kept is the part that matters: a request that fails is retried a few
times with exponential backoff, ``Retry-After`` is honored when the server asks
for it, and door releases are never retried at all, because firing a door twice
after a slow response is worse than failing.  The goal is not to guess a limit
but to make sure a fault on our side cannot turn into a request flood.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from json import loads as json_loads
import logging
import random
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .auth import ButterflyMXAuth
from .const import (
    API_VERSION_PATH,
    BACKOFF_BASE,
    BACKOFF_MAX,
    MAX_RETRIES,
    PAGE_SIZE,
    REQUEST_TIMEOUT,
    V3_CONTENT_TYPE,
    V3_PATH,
    WEBHOOK_RESOURCE_CALL,
)
from .exceptions import (
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
    ButterflyMXRateLimitError,
    ButterflyMXResponseError,
)
from .models import (
    AccessLogEntry,
    AccessPoint,
    AccessTool,
    Call,
    CallHandle,
    Device,
    Keychain,
    Tenant,
    VirtualKey,
)

_LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _as_api_timestamp(value: datetime) -> str:
    """Render a datetime the way ButterflyMX's ``q[..._gteq]`` filters expect.

    Worth being careful here.  A filter the server cannot read is not rejected:
    sending ``q[logged_at_gteq]=not-a-date`` returns HTTP 200 and the *unfiltered*
    list.  So a bad timestamp does not fail, it silently widens the window to
    everything, and deduplication hides the symptom while every poll re-fetches
    the whole call log.

    The realistic way to get one is a naive or local-time datetime, which
    formats into a perfectly valid string for the wrong moment.  Converting to
    UTC first is what stops that.
    """
    if value.tzinfo is None:
        _LOGGER.warning(
            "A call query was given the naive timestamp %s; reading it as UTC. "
            "This is a bug, and the wrong reading would quietly shift which "
            "calls are considered new",
            value,
        )
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


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
    """Thin wrapper around the ButterflyMX v4 API."""

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
        base_path: str = API_VERSION_PATH,
        content_type: str | None = None,
    ) -> Any:
        """Perform an authenticated request and return the decoded body."""
        url = f"{self._api_url}{base_path}{path}"
        attempt = 0
        refreshed = False

        while True:
            attempt += 1
            token = await self._auth.async_get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": content_type or "application/json",
            }
            if content_type is not None and json is not None:
                # v3 speaks JSON:API and rejects a plain application/json body.
                headers["Content-Type"] = content_type

            try:
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
                #
                # This runs even when retries are disabled, and that is safe: a
                # 401 means the request was rejected outright, so a door release
                # that lands here did not open anything and can be sent again.
                if not refreshed:
                    refreshed = True
                    _LOGGER.debug("Got 401 on %s %s; forcing a token refresh", method, path)
                    # Hand back the token that was rejected so parallel requests
                    # hitting the same 401 do not each rotate the token pair.
                    await self._auth.async_force_refresh(token)
                    # Renewing a token is not a failed attempt, so it does not
                    # spend one of the retries meant for transient errors.
                    attempt -= 1
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
                # Parse the bytes already read above rather than calling
                # response.json(), which would only work because aiohttp
                # happens to cache the body.
                return json_loads(body)
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
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Collect every page of a list endpoint.

        Every list response carries ``page_info``, and ``next_page`` is null on
        the last one.  That is the whole termination condition; there is no
        arbitrary page cap, because a cap can only ever return a silently
        incomplete answer.  The one thing that would not terminate is a
        ``next_page`` that fails to advance, which would mean a broken response,
        so that is reported rather than followed.
        """
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            query: dict[str, Any] = dict(params or {})
            query["page"] = page
            query["per"] = PAGE_SIZE
            payload = await self._async_request("GET", path, params=query)
            if not isinstance(payload, dict):
                _LOGGER.warning(
                    "ButterflyMX returned a non-object body for GET %s page %s; "
                    "stopping after %d records",
                    path,
                    page,
                    len(collected),
                )
                break

            data = payload.get("data")
            if isinstance(data, list):
                collected.extend(item for item in data if isinstance(item, dict))

            page_info = payload.get("page_info") or {}
            raw_next = page_info.get("next_page")
            if raw_next is None:
                break
            try:
                next_page = int(raw_next)
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "ButterflyMX returned next_page=%r for GET %s, which is not "
                    "a page number; stopping after %d records",
                    raw_next,
                    path,
                    len(collected),
                )
                break
            if next_page <= page:
                _LOGGER.warning(
                    "ButterflyMX said page %s of GET %s is followed by page %s, "
                    "which does not move forward; stopping after %d records",
                    page,
                    path,
                    next_page,
                    len(collected),
                )
                break
            page = next_page

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

    async def async_get_access_tools(self) -> list[AccessTool]:
        """List the PINs and fobs on this account.

        Read so the access log can name what opened a door instead of printing
        an ID.  The response also carries each PIN in plaintext, which
        :class:`~custom_components.butterflymx.models.AccessTool` deliberately
        does not keep.
        """
        raw = await self._async_get_paginated("/access_tools")
        tools = [AccessTool.from_api(item) for item in raw]
        return [tool for tool in tools if tool is not None]

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
            params["q[logged_at_gteq]"] = _as_api_timestamp(since)
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

    async def async_get_access_logs(
        self, building_id: int, since: datetime | None = None, limit: int = 20
    ) -> list[AccessLogEntry]:
        """List recent door releases for a building, newest first.

        Scoped to the signed-in resident: on a live account every entry carried
        their own tenant and unit, so this does not report the neighbors.
        """
        params: dict[str, Any] = {"page": 1, "per": min(limit, PAGE_SIZE)}
        if since is not None:
            params["q[logged_at_gteq]"] = _as_api_timestamp(since)
        payload = await self._async_request(
            "GET", f"/buildings/{building_id}/access_logs", params=params
        )
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        entries = [
            AccessLogEntry.from_api(item) for item in data if isinstance(item, dict)
        ]
        return [entry for entry in entries if entry is not None]

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

    # -- Passes ---------------------------------------------------------------

    async def async_get_keychains(self) -> list[Keychain]:
        """List every pass on the account."""
        raw = await self._async_get_paginated("/keychains")
        keychains = [Keychain.from_api(item) for item in raw]
        return [keychain for keychain in keychains if keychain is not None]

    async def async_get_virtual_keys(
        self, keychain_id: int | None = None
    ) -> list[VirtualKey]:
        """List the credentials issued by one keychain, or by all of them."""
        params = (
            {"q[keychain_id_eq]": str(keychain_id)} if keychain_id is not None else None
        )
        raw = await self._async_get_paginated("/virtual_keys", params)
        keys = [VirtualKey.from_api(item) for item in raw]
        return [key for key in keys if key is not None]

    async def async_create_delivery_pass(self, tenant_id: int, name: str) -> Keychain:
        """Create a single-use delivery pass.

        The server fills in everything else: it starts now, runs for 30 days,
        opens every door on the account, and issues its own virtual key.  There
        is nothing else to send, which is why this takes no window and no doors.
        """
        return await self._async_create_keychain(
            "delivery_pass", {"name": name, "tenant_id": tenant_id}
        )

    async def async_create_visitor_pass(
        self,
        tenant_id: int,
        name: str,
        starts_at: datetime,
        ends_at: datetime,
        access_point_ids: list[int] | None = None,
        device_ids: list[int] | None = None,
    ) -> Keychain:
        """Create a reusable pass valid over a window.

        Leaving the doors empty grants all of them, which is what ButterflyMX
        does for a delivery pass too.

        ``recipients`` is deliberately not accepted.  Passing it makes
        ButterflyMX email or text the addresses given, so a service that took it
        could send a working door code to a stranger on a typo.  The codes come
        back in the response instead, to be handed out however the caller likes.
        """
        body: dict[str, Any] = {
            "name": name,
            "tenant_id": tenant_id,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        }
        if access_point_ids:
            body["access_point_ids"] = access_point_ids
        if device_ids:
            body["device_ids"] = device_ids
        return await self._async_create_keychain("custom", body)

    async def _async_create_keychain(
        self, kind: str, attributes: dict[str, Any]
    ) -> Keychain:
        """POST one of the keychain sub-resources and parse what comes back."""
        payload = await self._async_request(
            "POST", f"/keychains/{kind}", json={"keychain": attributes}, retry=False
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        keychain = Keychain.from_api(data) if isinstance(data, dict) else None
        if keychain is None:
            raise ButterflyMXResponseError(
                f"ButterflyMX accepted the {kind} pass but did not describe it"
            )
        return keychain

    async def async_create_virtual_key(
        self, keychain_id: int, name: str | None = None
    ) -> VirtualKey:
        """Issue a credential on an existing keychain.

        A custom keychain arrives with ``virtual_key_ids: []`` -- it is the
        grant, not the code -- so a visitor pass is only usable once this has
        run.  A delivery pass issues its own and does not need this.
        """
        attributes: dict[str, Any] = {"keychain_id": keychain_id}
        if name:
            attributes["name"] = name
        payload = await self._async_request(
            "POST", "/virtual_keys", json={"virtual_key": attributes}, retry=False
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        key = VirtualKey.from_api(data) if isinstance(data, dict) else None
        if key is None:
            raise ButterflyMXResponseError(
                "ButterflyMX created the code but did not return it"
            )
        return key

    async def async_delete_keychain(self, keychain_id: int) -> None:
        """Revoke a pass, and with it every code it issued."""
        await self._async_request("DELETE", f"/keychains/{keychain_id}", retry=False)

    # -- Telling the panel a call was handled ---------------------------------

    async def async_get_call_handle(self, call_id: int) -> CallHandle | None:
        """Find the guid and panel for a call, so the panel can be told about it.

        v4 does not carry either, and they are what the panel is addressed by.
        The two APIs number calls the same way, verified on a live account, so
        the ID this integration already has is enough to find the rest.

        Not retried.  This is only worth knowing while the call is still up, and
        backing off exponentially would spend longer than the panel rings for.
        """
        payload = await self._async_request(
            "GET",
            "/me/calls",
            retry=False,
            base_path=V3_PATH,
            content_type=V3_CONTENT_TYPE,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        for item in data:
            handle = CallHandle.from_v3(item)
            if handle is not None and handle.call_id == call_id:
                return handle
        _LOGGER.debug("No v3 record for call %s; cannot notify the panel", call_id)
        return None

    async def async_notify_panel(self, command: str, handle: CallHandle) -> None:
        """Tell the panel a call has been handled.

        Without this the panel keeps dialing and rolls over to a phone call,
        even though the door has already been opened.  Releasing a door and
        telling the panel about it are two separate things, and only the first
        one is in v4.

        Not retried: these are only worth sending while the call is still up,
        and a late duplicate would be acting on a call that has moved on.
        """
        await self._async_request(
            "POST",
            f"/notifications/{command}",
            json={
                "data": {
                    "type": "notifications",
                    "attributes": {
                        "call_guid": handle.guid,
                        "source_id": handle.panel_id,
                        "video": False,
                        "audio": False,
                    },
                }
            },
            retry=False,
            base_path=V3_PATH,
            content_type=V3_CONTENT_TYPE,
        )

    # -- Images ---------------------------------------------------------------

    async def async_fetch_image(self, url: str) -> bytes | None:
        """Download a call snapshot.

        Sent without credentials, deliberately.  A snapshot URL points straight
        at a public S3 object with no signature on it, and attaching a bearer
        token makes S3 reject the request outright, so authenticating here would
        only break the download.

        Worth knowing, and not something this integration can fix: that URL is
        the picture.  Anyone holding it can see who was at the door, with no
        credentials and no expiry, which is why it is redacted everywhere it
        would otherwise be written down.
        """
        try:
            response = await self._session.get(
                url, timeout=ClientTimeout(total=REQUEST_TIMEOUT)
            )
            if response.status >= 400:
                _LOGGER.debug("Snapshot download failed with HTTP %s", response.status)
                return None
            return await response.read()
        except (TimeoutError, ClientError) as err:
            _LOGGER.debug("Snapshot download failed: %s", err)
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
