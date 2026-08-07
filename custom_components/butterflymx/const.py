"""Constants for the ButterflyMX integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "butterflymx"

MANUFACTURER: Final = "ButterflyMX"

# --- Configuration keys -------------------------------------------------------

CONF_ENVIRONMENT: Final = "environment"
CONF_ACCOUNTS_URL: Final = "accounts_url"
CONF_API_URL: Final = "api_url"
CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_REDIRECT_URI: Final = "redirect_uri"
CONF_TOKEN: Final = "token"
CONF_AUTH_CODE: Final = "auth_code"

CONF_CALL_SCAN_INTERVAL: Final = "call_scan_interval"
CONF_RELOCK_DELAY: Final = "relock_delay"
CONF_ENABLE_WEBHOOK: Final = "enable_webhook"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_WEBHOOK_INTEGRATION_IDS: Final = "webhook_integration_ids"

# --- Credentials ----------------------------------------------------------------

# The OAuth client this integration authorizes as.
#
# Leave empty and setup asks the user for their own client ID, which means
# applying to ButterflyMX's developer programme first.  Fill it in and setup
# skips that step entirely.
#
# It is not a secret and cannot be one.  ButterflyMX issues public clients, so
# there is nothing to keep alongside it, and a client ID has to reach the
# browser to start the authorization anyway.  The same value is sitting in
# ButterflyMX's own Android app for anyone who cares to look.  What protects an
# account is the sign-in and the PKCE challenge, neither of which this touches.
DEFAULT_CLIENT_ID: Final = ""

# --- Environments -------------------------------------------------------------

# Production is the only environment the integration offers.  Sandbox exists for
# development against a throwaway account and is deliberately not selectable in
# the UI: nobody installing this wants their real building talking to it.  Use
# scripts/probe_api.py --env sandbox for that instead.
ENV_PRODUCTION: Final = "production"
ENV_SANDBOX: Final = "sandbox"

# The OAuth2 authorization server is a separate host from the REST API.
ENVIRONMENTS: Final[dict[str, dict[str, str]]] = {
    ENV_PRODUCTION: {
        CONF_ACCOUNTS_URL: "https://accounts.butterflymx.com",
        CONF_API_URL: "https://api.butterflymx.com",
    },
    ENV_SANDBOX: {
        CONF_ACCOUNTS_URL: "https://accounts.na.sandbox.butterflymx.com",
        CONF_API_URL: "https://api.na.sandbox.butterflymx.com",
    },
}

OAUTH2_AUTHORIZE_PATH: Final = "/oauth/authorize"
OAUTH2_TOKEN_PATH: Final = "/oauth/token"

# ButterflyMX documents this "out of band" redirect URI as the default for
# development credentials.  It displays the authorization code in the browser
# instead of redirecting, which is what the config flow's paste step expects.
OOB_REDIRECT_URI: Final = "urn:ietf:wg:oauth:2.0:oob"

# --- API ----------------------------------------------------------------------

API_VERSION_PATH: Final = "/v4"

# Access token lifetime is documented as 24h; refresh this far ahead of expiry.
TOKEN_EXPIRY_MARGIN: Final = 300  # seconds

# Nothing here paces requests.  No rate limits are published, none are returned
# in response headers, and requests are issued one at a time anyway.  These
# values only govern what happens after something goes wrong.
MAX_RETRIES: Final = 3
BACKOFF_BASE: Final = 1.5  # seconds
BACKOFF_MAX: Final = 30.0  # seconds
REQUEST_TIMEOUT: Final = 30  # seconds
PAGE_SIZE: Final = 100

# Door release requests are not idempotent, so they are never auto-retried, and
# the same door will not be fired more often than this.
DOOR_RELEASE_COOLDOWN: Final = 3.0  # seconds

# --- Polling ------------------------------------------------------------------

DEFAULT_CALL_SCAN_INTERVAL: Final = 10  # seconds
MIN_CALL_SCAN_INTERVAL: Final = 5
MAX_CALL_SCAN_INTERVAL: Final = 300

# Buildings/access points/devices change rarely.
TOPOLOGY_SCAN_INTERVAL: Final = 3600  # seconds

# Door releases are history rather than something to react to in the moment: a
# visitor at the door is the call, and that has its own fast loop.  Polled at a
# slower rate so adding it does not double the request count.
ACCESS_LOG_SCAN_INTERVAL: Final = 60  # seconds

# How far back to look for door releases on the first poll after startup.
ACCESS_LOG_LOOKBACK: Final = 300  # seconds

# Visitor and delivery passes only change when someone changes them, and every
# service that does asks for a refresh immediately.  This interval exists to
# notice passes created or deleted in the ButterflyMX app, which does not need
# to be quick.
PASS_SCAN_INTERVAL: Final = 900  # seconds

# When ButterflyMX is pushing calls to us, polling stops being the way calls are
# noticed and becomes a safety net, so it slows right down.  It cannot be turned
# off: deliveries are not guaranteed, nothing arrives while Home Assistant is
# restarting, and a registration whose external URL has changed fails silently.
# A slow poll repairs all three.
WEBHOOK_FALLBACK_SCAN_INTERVAL: Final = 300  # seconds

# How far back to look for calls on the first poll after startup.
CALL_LOOKBACK: Final = 300  # seconds

# Each poll asks for calls logged since the previous poll started, minus this
# much.  Our clock and ButterflyMX's are not the same clock, and a call logged
# while a request was in flight would fall between two windows and never be
# seen.  Re-reading a few seconds of overlap is free, since calls already
# announced are filtered out by ID.
CALL_POLL_OVERLAP: Final = 30  # seconds

DEFAULT_RELOCK_DELAY: Final = 5  # seconds
MIN_RELOCK_DELAY: Final = 1
MAX_RELOCK_DELAY: Final = 300

# --- Device types -------------------------------------------------------------

# Doors always belong to a building, never to a unit.  A tenancy only supplies
# the identity a release is performed as, and the unit only says who a visitor
# was calling.
#
# Most doors arrive as access points.  These device "type" values are the ones
# released by device_id instead, so they are picked up separately.  Note that a
# door release sent with device_id opens *every* access point on that device,
# whereas access_point_id opens exactly one.
#
# The API documents ten device types: cloud_based_access_controller, keypad,
# smart_lock, panel, virtual_intercom, elevator_control, key_locker, elevator,
# front_desk_station and remote_lock.  Only the two lock types are exposed;
# the rest are either reachable as access points already or are not doors.
DIRECT_LOCK_DEVICE_TYPES: Final[frozenset[str]] = frozenset(
    {"smart_lock", "remote_lock"}
)

# --- Events -------------------------------------------------------------------

EVENT_CALL: Final = "butterflymx_call"
EVENT_TYPE_CALL: Final = "call"

EVENT_DOOR_RELEASE: Final = "butterflymx_door_release"
EVENT_TYPE_DOOR_RELEASE: Final = "door_release"

WEBHOOK_RESOURCE_CALL: Final = "call"
WEBHOOK_RESOURCE_DOOR_RELEASE: Final = "door_release"

# --- Services -----------------------------------------------------------------

# Named after what the ButterflyMX app calls them, so the two agree.  A delivery
# pass is single use; a visitor pass is reusable inside its window.
SERVICE_CREATE_DELIVERY_PASS: Final = "create_delivery_pass"
SERVICE_CREATE_VISITOR_PASS: Final = "create_visitor_pass"
SERVICE_LIST_PASSES: Final = "list_passes"
SERVICE_REVOKE_PASS: Final = "revoke_pass"

ATTR_PASS_ID: Final = "pass_id"
ATTR_DOORS: Final = "doors"
ATTR_STARTS_AT: Final = "starts_at"
ATTR_ENDS_AT: Final = "ends_at"
ATTR_PASSES: Final = "passes"

# A visitor pass with no end given runs for this long.
DEFAULT_VISITOR_PASS_HOURS: Final = 4
