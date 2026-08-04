#!/usr/bin/env python3
"""Probe the real ButterflyMX API and report what it actually returns.

This is a development tool, not part of the integration.  It answers the
questions the published documentation leaves open, so the integration's
assumptions can be checked against a live sandbox account before any of it is
pointed at a real building:

1. Does ``GET /v4/tenants?scope=self`` return the signed-in resident's tenancy?
2. Can a resident enumerate ``GET /v4/devices``, or is that admin-only?
3. Do call snapshot URLs need the bearer token, or are they pre-signed?
4. What values actually appear in a call's ``notification_type`` and ``status``?

Standard library only - it runs with any Python 3.11+, no virtualenv needed.

Usage:

    export BMX_CLIENT_ID=...        # or pass --client-id
    export BMX_CLIENT_SECRET=...    # or pass --client-secret
    python scripts/probe_api.py --env sandbox

On the first run it prints a ButterflyMX sign-in link, waits for you to paste
back the authorization code, and caches the resulting token in
``scripts/.bmx-token-<env>.json`` (gitignored) so later runs skip that step.

Findings are printed to the terminal and a redacted transcript is written to
``scripts/probe-output-<env>.json``.  Names, emails, serial numbers and tokens
are replaced before anything is written to disk, so the file is safe to paste
into an issue.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ENVIRONMENTS = {
    "sandbox": {
        "accounts": "https://accounts.na.sandbox.butterflymx.com",
        "api": "https://api.na.sandbox.butterflymx.com",
    },
    "production": {
        "accounts": "https://accounts.butterflymx.com",
        "api": "https://api.butterflymx.com",
    },
}

OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
TIMEOUT = 30

SCRIPT_DIR = Path(__file__).resolve().parent

# Keys the integration's models read.  Anything missing here is a bug waiting
# to happen; anything extra is a possible feature we are not using yet.
EXPECTED_KEYS = {
    "tenant": {"id", "building_id", "building_name", "email", "unit"},
    "access_point": {"id", "building_id", "name", "device_ids"},
    "device": {"id", "building_id", "name", "type"},
    "call": {"id", "logged_at", "notification_type", "device", "image_url"},
}

REDACT_KEYS = {
    "email",
    "first_name",
    "last_name",
    "full_name",
    "phone",
    "phone_number",
    "serial_number",
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
}


class ProbeError(Exception):
    """Raised when the API could not be reached or understood."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _request(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    token: str | None = None,
    raw: bool = False,
) -> tuple[int, Any]:
    """Perform a request and return ``(status, body)`` without raising on 4xx."""
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as err:
        payload = err.read()
        status = err.code
    except urllib.error.URLError as err:
        raise ProbeError(f"Could not reach {url}: {err.reason}") from err

    if raw:
        return status, payload
    if not payload:
        return status, None
    try:
        return status, json.loads(payload)
    except json.JSONDecodeError:
        return status, payload[:500].decode("utf-8", "replace")


def _api_get(api_url: str, path: str, token: str, **params: Any) -> tuple[int, Any]:
    """GET a v4 endpoint."""
    query = urllib.parse.urlencode(params) if params else ""
    url = f"{api_url}/v4{path}" + (f"?{query}" if query else "")
    return _request("GET", url, token=token)


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #


def _token_path(env: str) -> Path:
    return SCRIPT_DIR / f".bmx-token-{env}.json"


def _load_token(env: str) -> dict[str, Any] | None:
    path = _token_path(env)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_token(env: str, token: dict[str, Any]) -> None:
    path = _token_path(env)
    path.write_text(json.dumps(token, indent=2))
    print(f"  token cached in {path.name}")


def _normalize(token: dict[str, Any]) -> dict[str, Any]:
    token = dict(token)
    token["expires_at"] = time.time() + float(token.get("expires_in") or 0)
    return token


def authorize(accounts_url: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Walk the authorization-code flow interactively."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": OOB_REDIRECT_URI,
            "response_type": "code",
        }
    )
    print("\nOpen this URL, sign in to ButterflyMX and approve access:\n")
    print(f"  {accounts_url}/oauth/authorize?{query}\n")
    code = input("Paste the authorization code (or the whole redirect URL): ").strip()
    if "code=" in code:
        parsed = urllib.parse.urlparse(code)
        found = urllib.parse.parse_qs(parsed.query or parsed.fragment).get("code")
        if found:
            code = found[0]

    status, payload = _request(
        "POST",
        f"{accounts_url}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": OOB_REDIRECT_URI,
        },
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        raise ProbeError(f"Token exchange failed (HTTP {status}): {payload}")
    return _normalize(payload)


def refresh(
    accounts_url: str, client_id: str, client_secret: str, token: dict[str, Any]
) -> dict[str, Any]:
    """Exchange the refresh token for a new pair."""
    status, payload = _request(
        "POST",
        f"{accounts_url}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        raise ProbeError(f"Token refresh failed (HTTP {status}): {payload}")
    new_token = _normalize(payload)
    new_token.setdefault("refresh_token", token["refresh_token"])
    return new_token


def get_token(env: str, accounts_url: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Load, refresh or obtain a token."""
    token = _load_token(env)
    if token and token.get("expires_at", 0) - 300 > time.time():
        print("Using cached access token.")
        return token
    if token and token.get("refresh_token"):
        print("Cached token expired; refreshing...")
        try:
            token = refresh(accounts_url, client_id, client_secret, token)
        except ProbeError as err:
            print(f"  refresh failed ({err}); starting a fresh authorization")
        else:
            _save_token(env, token)
            print("  refresh succeeded - refresh tokens work as documented")
            return token

    token = authorize(accounts_url, client_id, client_secret)
    _save_token(env, token)
    return token


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def redact(value: Any) -> Any:
    """Strip anything personal before writing the transcript to disk."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in REDACT_KEYS and item is not None:
                out[key] = "**redacted**"
            elif key in ("image_url", "url") and isinstance(item, str):
                out[key] = _redact_url(item)
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _redact_url(url: str) -> str:
    """Keep the shape of a URL but drop any signature parameters."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url
    keys = sorted(urllib.parse.parse_qs(parsed.query))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<params: {', '.join(keys)}>"


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def _keys_of(items: Iterable[Any]) -> set[str]:
    keys: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            keys.update(item)
    return keys


def _report_keys(label: str, items: list[Any], findings: list[str]) -> None:
    """Compare the keys the API sent against the ones the integration reads."""
    expected = EXPECTED_KEYS.get(label, set())
    actual = _keys_of(items)
    if not actual:
        return
    missing = expected - actual
    if missing:
        findings.append(f"!! {label}: expected keys absent from the response: {sorted(missing)}")
    extra = actual - expected
    if extra:
        findings.append(f"   {label}: additional keys available: {sorted(extra)}")


def probe(api_url: str, token: str) -> tuple[dict[str, Any], list[str]]:
    """Run every probe and return the transcript plus human-readable findings."""
    transcript: dict[str, Any] = {}
    findings: list[str] = []

    # Q1 - does scope=self work, and how does it differ from an unscoped list?
    print("\n[1/5] GET /v4/tenants?scope=self")
    status, scoped = _api_get(api_url, "/tenants", token, scope="self", per=100)
    transcript["tenants_self"] = {"status": status, "body": redact(scoped)}
    scoped_rows = scoped.get("data", []) if isinstance(scoped, dict) else []
    print(f"      HTTP {status}, {len(scoped_rows)} tenant record(s)")

    status_all, unscoped = _api_get(api_url, "/tenants", token, per=100)
    transcript["tenants_all"] = {"status": status_all, "body": redact(unscoped)}
    unscoped_rows = unscoped.get("data", []) if isinstance(unscoped, dict) else []
    print(f"      unscoped: HTTP {status_all}, {len(unscoped_rows)} record(s)")

    if status != 200:
        findings.append(
            f"!! /v4/tenants?scope=self returned HTTP {status} - the integration cannot start"
        )
    elif not scoped_rows:
        findings.append(
            "!! scope=self returned no tenants. The integration treats this as a "
            "setup failure; it may need to fall back to an unscoped list."
        )
    else:
        findings.append(f"OK scope=self returns {len(scoped_rows)} tenancy record(s)")
        if len(unscoped_rows) > len(scoped_rows):
            findings.append(
                f"   note: unscoped returns {len(unscoped_rows)}; "
                "scope=self is doing real filtering"
            )
    _report_keys("tenant", scoped_rows or unscoped_rows, findings)

    building_ids = sorted(
        {
            row["building_id"]
            for row in scoped_rows
            if isinstance(row, dict) and row.get("building_id")
        }
    )
    if not building_ids:
        findings.append("!! No building_id found - the remaining probes cannot run")
        return transcript, findings

    building_id = building_ids[0]
    print(f"      buildings: {building_ids}")

    # Access points - the primary source of lock entities.
    print(f"\n[2/5] GET /v4/access_points (building {building_id})")
    status, points = _api_get(
        api_url, "/access_points", token, per=100, **{"q[building_id_eq]": str(building_id)}
    )
    transcript["access_points"] = {"status": status, "body": redact(points)}
    point_rows = points.get("data", []) if isinstance(points, dict) else []
    print(f"      HTTP {status}, {len(point_rows)} access point(s)")
    if status != 200:
        findings.append(f"!! /v4/access_points returned HTTP {status} - no locks would be created")
    else:
        findings.append(f"OK {len(point_rows)} access point(s) -> {len(point_rows)} lock entities")
        for row in point_rows:
            if isinstance(row, dict):
                print(f"        - {row.get('name')!r} (id {row.get('id')})")
    _report_keys("access_point", point_rows, findings)

    # Q2 - can a resident enumerate devices?
    print(f"\n[3/5] GET /v4/devices (building {building_id})")
    status, devices = _api_get(
        api_url, "/devices", token, per=100, **{"q[building_id_eq]": str(building_id)}
    )
    transcript["devices"] = {"status": status, "body": redact(devices)}
    device_rows = devices.get("data", []) if isinstance(devices, dict) else []
    print(f"      HTTP {status}, {len(device_rows)} device(s)")
    if status == 200:
        types = sorted({row.get("type") for row in device_rows if isinstance(row, dict)} - {None})
        findings.append(f"OK residents can list devices; types seen: {types}")
        if not any(t in ("smart_lock", "remote_lock") for t in types):
            findings.append(
                "   no smart_lock/remote_lock devices - only access-point locks will appear"
            )
    else:
        findings.append(
            f"   /v4/devices returned HTTP {status} - residents cannot enumerate devices. "
            "The integration already degrades gracefully, but unit smart locks would be invisible."
        )
    _report_keys("device", device_rows, findings)

    # Q4 - what a real call looks like.
    print(f"\n[4/5] GET /v4/buildings/{building_id}/calls")
    status, calls = _api_get(api_url, f"/buildings/{building_id}/calls", token, per=50)
    transcript["calls"] = {"status": status, "body": redact(calls)}
    call_rows = calls.get("data", []) if isinstance(calls, dict) else []
    print(f"      HTTP {status}, {len(call_rows)} call(s)")
    if status != 200:
        findings.append(
            f"!! /v4/buildings/{{id}}/calls returned HTTP {status} - no doorbell events"
        )
    elif not call_rows:
        findings.append(
            "   no calls in the log yet. Place a test call from the sandbox intercom and "
            "re-run to capture a real payload."
        )
    else:
        notification_types = sorted(
            {row.get("notification_type") for row in call_rows if isinstance(row, dict)} - {None}
        )
        statuses = sorted(
            {row.get("status") for row in call_rows if isinstance(row, dict)} - {None}
        )
        findings.append(f"OK notification_type values seen: {notification_types}")
        findings.append(f"OK status values seen: {statuses}")
        recipients = sorted(
            {
                (row.get("recipient") or {}).get("type")
                for row in call_rows
                if isinstance(row, dict) and isinstance(row.get("recipient"), dict)
            }
            - {None}
        )
        findings.append(f"OK recipient types seen: {recipients}")
    _report_keys("call", call_rows, findings)

    # Q3 - do snapshot URLs need credentials?
    print("\n[5/5] Snapshot URL authentication")
    image_url = next(
        (
            row["image_url"]
            for row in call_rows
            if isinstance(row, dict) and row.get("image_url")
        ),
        None,
    )
    if not image_url:
        print("      skipped: no call with an image_url")
        findings.append("   snapshot auth untested - no call in the log carried an image_url")
    else:
        anon_status, anon_body = _request("GET", image_url, raw=True)
        auth_status, auth_body = _request("GET", image_url, token=token, raw=True)
        transcript["snapshot"] = {
            "url": _redact_url(image_url),
            "anonymous_status": anon_status,
            "anonymous_bytes": len(anon_body) if isinstance(anon_body, bytes) else 0,
            "authenticated_status": auth_status,
            "authenticated_bytes": len(auth_body) if isinstance(auth_body, bytes) else 0,
        }
        print(f"      without token: HTTP {anon_status}")
        print(f"      with token:    HTTP {auth_status}")
        if anon_status == 200:
            findings.append(
                "OK snapshot URLs are pre-signed; no token needed (matches the integration)"
            )
        elif auth_status == 200:
            findings.append(
                "OK snapshot URLs require the bearer token. The integration's retry-with-auth "
                "path handles this, but it costs an extra request per image."
            )
        else:
            findings.append(
                f"!! snapshot URL failed both ways ({anon_status}/{auth_status}) - "
                "the image entity will stay unavailable"
            )

    return transcript, findings


# --------------------------------------------------------------------------- #


def main() -> int:
    """Run the probe."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--env", choices=sorted(ENVIRONMENTS), default="sandbox")
    parser.add_argument("--client-id", default=os.environ.get("BMX_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("BMX_CLIENT_SECRET"))
    parser.add_argument(
        "--reset", action="store_true", help="discard the cached token and re-authorize"
    )
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        parser.error("set BMX_CLIENT_ID and BMX_CLIENT_SECRET, or pass --client-id/--client-secret")

    urls = ENVIRONMENTS[args.env]
    print(f"ButterflyMX probe - {args.env}")
    print(f"  accounts: {urls['accounts']}")
    print(f"  api:      {urls['api']}")

    if args.reset:
        _token_path(args.env).unlink(missing_ok=True)

    try:
        token = get_token(args.env, urls["accounts"], args.client_id, args.client_secret)
        transcript, findings = probe(urls["api"], token["access_token"])
    except ProbeError as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        return 1

    out_path = SCRIPT_DIR / f"probe-output-{args.env}.json"
    out_path.write_text(json.dumps(transcript, indent=2, sort_keys=True))

    print("\n" + "=" * 72)
    print("FINDINGS")
    print("=" * 72)
    for line in findings:
        print(line)
    print("=" * 72)
    print(f"\nRedacted transcript written to {out_path}")
    print("Names, emails, serial numbers and URL signatures have been removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
