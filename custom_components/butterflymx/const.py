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

# --- Environments -------------------------------------------------------------

ENV_PRODUCTION: Final = "production"
ENV_SANDBOX: Final = "sandbox"
ENV_CUSTOM: Final = "custom"

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

# No rate limits are documented, so be conservative and self-throttle.
MAX_CONCURRENT_REQUESTS: Final = 4
MIN_REQUEST_INTERVAL: Final = 0.25  # seconds between requests
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

# How far back to look for calls on the first poll after startup.
CALL_LOOKBACK: Final = 300  # seconds

DEFAULT_RELOCK_DELAY: Final = 5  # seconds
MIN_RELOCK_DELAY: Final = 1
MAX_RELOCK_DELAY: Final = 300

# --- Device types -------------------------------------------------------------

# Device "type" values that represent a lock a tenant can release directly.
# Access points cover intercoms/ACS controllers/keypads/common-area locks; these
# are the unit-level smart locks that are addressed by device_id instead.
UNIT_LOCK_DEVICE_TYPES: Final[frozenset[str]] = frozenset(
    {"smart_lock", "remote_lock"}
)

# --- Events -------------------------------------------------------------------

EVENT_CALL: Final = "butterflymx_call"
EVENT_TYPE_CALL: Final = "call"

WEBHOOK_RESOURCE_CALL: Final = "call"
WEBHOOK_RESOURCE_DOOR_RELEASE: Final = "door_release"
