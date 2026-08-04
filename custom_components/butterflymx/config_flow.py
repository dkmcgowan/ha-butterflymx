"""Config flow for ButterflyMX.

ButterflyMX hands out developer credentials whose default redirect URI is the
out-of-band value ``urn:ietf:wg:oauth:2.0:oob``, which shows the authorization
code in the browser instead of redirecting anywhere.  Home Assistant's built-in
OAuth helper always redirects, so this flow does the exchange itself: it builds
the authorize URL, the user signs in to ButterflyMX, and pastes back either the
code or the full redirect URL they landed on.  That covers both OOB credentials
and accounts that have had a custom redirect URI registered.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import ButterflyMXClient
from .auth import ButterflyMXAuth, async_exchange_code, build_authorize_url
from .const import (
    CONF_ACCOUNTS_URL,
    CONF_API_URL,
    CONF_AUTH_CODE,
    CONF_CALL_SCAN_INTERVAL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_WEBHOOK,
    CONF_ENVIRONMENT,
    CONF_REDIRECT_URI,
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    DEFAULT_CALL_SCAN_INTERVAL,
    DEFAULT_RELOCK_DELAY,
    DOMAIN,
    ENV_CUSTOM,
    ENV_PRODUCTION,
    ENV_SANDBOX,
    ENVIRONMENTS,
    MAX_CALL_SCAN_INTERVAL,
    MAX_RELOCK_DELAY,
    MIN_CALL_SCAN_INTERVAL,
    MIN_RELOCK_DELAY,
    OOB_REDIRECT_URI,
)
from .exceptions import ButterflyMXAuthError, ButterflyMXConnectionError, ButterflyMXError

_LOGGER = logging.getLogger(__name__)


def extract_code(raw: str) -> str:
    """Return the authorization code from a pasted code or redirect URL."""
    value = raw.strip()
    if "://" not in value and "code=" not in value:
        return value
    parsed = urlparse(value)
    for source in (parsed.query, parsed.fragment):
        if not source:
            continue
        codes = parse_qs(source).get("code")
        if codes:
            return codes[0].strip()
    return value


class ButterflyMXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ButterflyMX config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self._environment: str = ENV_PRODUCTION
        self._accounts_url: str = ENVIRONMENTS[ENV_PRODUCTION][CONF_ACCOUNTS_URL]
        self._api_url: str = ENVIRONMENTS[ENV_PRODUCTION][CONF_API_URL]
        self._client_id: str = ""
        self._client_secret: str = ""
        self._redirect_uri: str = OOB_REDIRECT_URI
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ButterflyMXOptionsFlow:
        """Return the options flow."""
        return ButterflyMXOptionsFlow()

    # -- Initial setup --------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which ButterflyMX environment to talk to."""
        if user_input is not None:
            self._environment = user_input[CONF_ENVIRONMENT]
            if self._environment == ENV_CUSTOM:
                return await self.async_step_endpoints()
            urls = ENVIRONMENTS[self._environment]
            self._accounts_url = urls[CONF_ACCOUNTS_URL]
            self._api_url = urls[CONF_API_URL]
            return await self.async_step_credentials()

        schema = vol.Schema(
            {
                vol.Required(CONF_ENVIRONMENT, default=ENV_PRODUCTION): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[ENV_PRODUCTION, ENV_SANDBOX, ENV_CUSTOM],
                        translation_key="environment",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_endpoints(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect custom accounts/API hostnames."""
        errors: dict[str, str] = {}
        if user_input is not None:
            accounts_url = user_input[CONF_ACCOUNTS_URL].strip().rstrip("/")
            api_url = user_input[CONF_API_URL].strip().rstrip("/")
            if not accounts_url.startswith("http") or not api_url.startswith("http"):
                errors["base"] = "invalid_url"
            else:
                self._accounts_url = accounts_url
                self._api_url = api_url
                return await self.async_step_credentials()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ACCOUNTS_URL,
                    default=user_input.get(CONF_ACCOUNTS_URL, self._accounts_url)
                    if user_input
                    else self._accounts_url,
                ): str,
                vol.Required(
                    CONF_API_URL,
                    default=user_input.get(CONF_API_URL, self._api_url)
                    if user_input
                    else self._api_url,
                ): str,
            }
        )
        return self.async_show_form(
            step_id="endpoints", data_schema=schema, errors=errors
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the ButterflyMX API client credentials."""
        if user_input is not None:
            self._client_id = user_input[CONF_CLIENT_ID].strip()
            self._client_secret = user_input[CONF_CLIENT_SECRET].strip()
            self._redirect_uri = (
                user_input.get(CONF_REDIRECT_URI) or OOB_REDIRECT_URI
            ).strip()
            return await self.async_step_authorize()

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIENT_ID, default=self._client_id): str,
                vol.Required(CONF_CLIENT_SECRET, default=self._client_secret): str,
                vol.Optional(CONF_REDIRECT_URI, default=self._redirect_uri): str,
            }
        )
        return self.async_show_form(step_id="credentials", data_schema=schema)

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the authorize URL and accept the resulting code."""
        errors: dict[str, str] = {}
        authorize_url = build_authorize_url(
            self._accounts_url, self._client_id, self._client_secret, self._redirect_uri
        )

        if user_input is not None:
            code = extract_code(user_input[CONF_AUTH_CODE])
            if not code:
                errors["base"] = "invalid_auth_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    token = await async_exchange_code(
                        session,
                        self._accounts_url,
                        self._client_id,
                        self._client_secret,
                        code,
                        self._redirect_uri,
                    )
                    account_id, account_name = await self._async_probe_account(token)
                except ButterflyMXAuthError:
                    errors["base"] = "invalid_auth"
                except ButterflyMXConnectionError:
                    errors["base"] = "cannot_connect"
                except ButterflyMXError:
                    _LOGGER.exception("Unexpected error linking ButterflyMX account")
                    errors["base"] = "unknown"
                else:
                    return await self._async_finish(token, account_id, account_name)

        schema = vol.Schema({vol.Required(CONF_AUTH_CODE): str})
        return self.async_show_form(
            step_id="authorize",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "authorize_url": authorize_url,
                "redirect_uri": self._redirect_uri,
            },
        )

    async def _async_probe_account(
        self, token: dict[str, Any]
    ) -> tuple[str, str]:
        """Call the API once to confirm the token works and name the entry."""
        session = async_get_clientsession(self.hass)
        auth = ButterflyMXAuth(
            session,
            self._accounts_url,
            self._client_id,
            self._client_secret,
            token,
        )
        client = ButterflyMXClient(session, self._api_url, auth)
        tenants = await client.async_get_tenants()
        if not tenants:
            raise ButterflyMXError("No tenant records returned for this account")

        tenant = tenants[0]
        account_id = (tenant.email or str(tenant.id)).lower()
        parts = [tenant.building_name, tenant.unit_label]
        detail = " ".join(part for part in parts if part)
        account_name = f"ButterflyMX {detail}".strip() if detail else "ButterflyMX"
        return account_id, account_name

    async def _async_finish(
        self, token: dict[str, Any], account_id: str, account_name: str
    ) -> ConfigFlowResult:
        """Create or update the config entry."""
        data = {
            CONF_ENVIRONMENT: self._environment,
            CONF_ACCOUNTS_URL: self._accounts_url,
            CONF_API_URL: self._api_url,
            CONF_CLIENT_ID: self._client_id,
            CONF_CLIENT_SECRET: self._client_secret,
            CONF_REDIRECT_URI: self._redirect_uri,
            CONF_TOKEN: token,
        }

        await self.async_set_unique_id(account_id)

        if self._reauth_entry is not None:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._reauth_entry, data={**self._reauth_entry.data, **data}
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=account_name, data=data)

    # -- Reauthentication -----------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token that can no longer be refreshed."""
        self._reauth_entry = self._get_reauth_entry()
        self._environment = entry_data.get(CONF_ENVIRONMENT, ENV_PRODUCTION)
        self._accounts_url = entry_data[CONF_ACCOUNTS_URL]
        self._api_url = entry_data[CONF_API_URL]
        self._client_id = entry_data[CONF_CLIENT_ID]
        self._client_secret = entry_data[CONF_CLIENT_SECRET]
        self._redirect_uri = entry_data.get(CONF_REDIRECT_URI, OOB_REDIRECT_URI)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user re-link, optionally with new credentials."""
        if user_input is not None:
            return await self.async_step_credentials()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"account": self._client_id},
        )


class ButterflyMXOptionsFlow(OptionsFlow):
    """Handle ButterflyMX options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling and behavior options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CALL_SCAN_INTERVAL,
                    default=options.get(
                        CONF_CALL_SCAN_INTERVAL, DEFAULT_CALL_SCAN_INTERVAL
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_CALL_SCAN_INTERVAL,
                        max=MAX_CALL_SCAN_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_RELOCK_DELAY,
                    default=options.get(CONF_RELOCK_DELAY, DEFAULT_RELOCK_DELAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_RELOCK_DELAY,
                        max=MAX_RELOCK_DELAY,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_ENABLE_WEBHOOK,
                    default=options.get(CONF_ENABLE_WEBHOOK, False),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
