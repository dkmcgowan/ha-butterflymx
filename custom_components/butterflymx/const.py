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
CONF_REDIRECT_URI: Final = "redirect_uri"
CONF_TOKEN: Final = "token"
CONF_AUTH_CODE: Final = "auth_code"

CONF_CALL_SCAN_INTERVAL: Final = "call_scan_interval"
CONF_ENABLE_WEBHOOK: Final = "enable_webhook"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_WEBHOOK_INTEGRATION_IDS: Final = "webhook_integration_ids"

# --- Credentials ----------------------------------------------------------------

# The OAuth client this integration authorizes as.
#
# Leave empty and setup asks the user for their own client ID, which means
# applying to ButterflyMX's developer program first.  Fill it in and setup
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

# Telling the panel a call has been handled is the one thing v4 cannot do, so
# those two requests go to v3.  Everything else here is v4.
#
# v3 is not in the published documentation, but it is a versioned API on the
# same host, it accepts the same OAuth token, and the two APIs are views of the
# same records: a call has the same ID in both.  What v4's call log leaves out
# is the call's `guid` and the panel that placed it, which is exactly what the
# panel needs to hear back.  So v4 stays the source of truth and v3 is consulted
# for those two fields and nothing else.
V3_PATH: Final = "/v3"
V3_CONTENT_TYPE: Final = "application/vnd.api+json"

# How long a door stays open after a release is configured per access point, and
# only the GraphQL API will say.  v4 has no field for it anywhere: a release
# takes no duration and returns none, and the access log records only that the
# door opened.  Without the real number the lock entity has to guess when to
# report itself locked again, and a door configured for 4 seconds and one
# configured for 14 look identical.
#
# Same host and same OAuth token as everything else here.  One read-only query
# on the hourly topology refresh, and the account works without it: a failure
# leaves every lock on its configured fallback rather than breaking setup.
GRAPHQL_PATH: Final = "/denizen/v1"
GRAPHQL_ENDPOINT: Final = "/graphql"

# The commands the official app sends when you act on a call from its
# notification: open_door when you let someone in, call_ended when you decline.
PANEL_COMMAND_OPEN_DOOR: Final = "open_door"
PANEL_COMMAND_CALL_ENDED: Final = "call_ended"

# How long after a call was logged it is still worth telling the panel about.
# Matches the 40 seconds the ButterflyMX app rings before giving up.
LIVE_CALL_WINDOW: Final = 40  # seconds

# Call statuses that mean the call is over, so there is no panel left to tell.
# Observed on a live account: initializing, canceled, timeout_online_signal,
# opened_door.
FINISHED_CALL_STATUSES: Final[frozenset[str]] = frozenset(
    {"canceled", "timeout_online_signal", "opened_door", "declined", "answered"}
)

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

# What to show a door as unlocked for when its real duration could not be read,
# which means either a door that is not an access point or a failed GraphQL
# read.  Deliberately not a setting.  Nobody knows their own door's hold time,
# which is the entire reason we go and ask, and the value is cosmetic: it moves
# when the lock icon flips back and nothing else.
FALLBACK_OPEN_SECONDS: Final = 5

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

# The event type a doorbell entity fires.  Home Assistant standardizes this as
# "ring" for anything with the doorbell device class, and warns on startup if it
# is missing.  Spelled out rather than imported from
# homeassistant.components.event.DoorbellEventType, which does not exist in the
# oldest Home Assistant this supports.
#
# Only the type string is "ring".  The payload still describes a ButterflyMX
# call, with the call ID, the device that placed it and the snapshot URL.
EVENT_TYPE_RING: Final = "ring"

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
SERVICE_DECLINE_CALL: Final = "decline_call"

ATTR_PASS_ID: Final = "pass_id"
ATTR_DOORS: Final = "doors"
ATTR_STARTS_AT: Final = "starts_at"
ATTR_ENDS_AT: Final = "ends_at"
ATTR_PASSES: Final = "passes"

# A visitor pass with no end given runs for this long.
DEFAULT_VISITOR_PASS_HOURS: Final = 4
