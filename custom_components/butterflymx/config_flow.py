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

from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
import voluptuous as vol

from .api import ButterflyMXClient
from .auth import (
    ButterflyMXAuth,
    async_exchange_code,
    build_authorize_url,
    new_code_verifier,
)
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
    CONF_WEBHOOK_ID,
    DEFAULT_CALL_SCAN_INTERVAL,
    DEFAULT_RELOCK_DELAY,
    DOMAIN,
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
    """Return the authorization code from a pasted code or redirect URL.

    Returns an empty string when something URL-shaped was pasted but holds no
    code, so the user is told their paste was wrong rather than being shown an
    authorization failure for a code that was never sent.
    """
    value = raw.strip()
    if "://" not in value and "code=" not in value:
        return value
    parsed = urlparse(value)
    # The last candidate covers a bare "code=..." fragment pasted on its own,
    # which urlparse reads as a path rather than a query.
    for source in (parsed.query, parsed.fragment, value):
        if not source:
            continue
        codes = parse_qs(source).get("code")
        if codes:
            return codes[0].strip()
    return ""


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
        # One verifier per flow.  It has to survive from building the authorize
        # URL to redeeming the code the user pastes back.
        self._code_verifier: str = new_code_verifier()

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
            urls = ENVIRONMENTS[self._environment]
            self._accounts_url = urls[CONF_ACCOUNTS_URL]
            self._api_url = urls[CONF_API_URL]
            return await self.async_step_credentials()

        schema = vol.Schema(
            {
                vol.Required(CONF_ENVIRONMENT, default=ENV_PRODUCTION): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[ENV_PRODUCTION, ENV_SANDBOX],
                        translation_key="environment",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the ButterflyMX API client credentials."""
        if user_input is not None:
            self._client_id = user_input[CONF_CLIENT_ID].strip()
            self._client_secret = (user_input.get(CONF_CLIENT_SECRET) or "").strip()
            self._redirect_uri = (
                user_input.get(CONF_REDIRECT_URI) or OOB_REDIRECT_URI
            ).strip()
            return await self.async_step_authorize()

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIENT_ID, default=self._client_id): str,
                # Optional: ButterflyMX issues public clients, which authorize
                # with PKCE and have no secret at all.
                vol.Optional(CONF_CLIENT_SECRET, default=self._client_secret): str,
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
            self._accounts_url,
            self._client_id,
            self._code_verifier,
            self._client_secret or None,
            self._redirect_uri,
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
                        code,
                        self._code_verifier,
                        self._client_secret or None,
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
        """Call the API once to confirm the token works and name the entry.

        The account ID becomes the entry's unique ID, so it has to come out the
        same on every reauthorization.  ButterflyMX promises no order for
        tenancies, and an account can hold several, so the lowest ID is used
        rather than whichever one happened to be listed first.  Getting this
        wrong on a multi-building account would tell the user their own
        credentials belong to somebody else, with no way past it.
        """
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

        tenant = min(tenants, key=lambda candidate: candidate.id)
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
            # Allocated now, whether or not push is ever switched on, so the
            # options screen can show the exact URL that would be registered
            # rather than describing one.
            CONF_WEBHOOK_ID: webhook.async_generate_id(),
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
        self._client_secret = entry_data.get(CONF_CLIENT_SECRET) or ""
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
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_CALL_SCAN_INTERVAL,
                            max=MAX_CALL_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    # A number selector hands back a float, and these are whole
                    # seconds, so keep 10 out of the entry as 10.0.
                    vol.Coerce(int),
                ),
                vol.Required(
                    CONF_RELOCK_DELAY,
                    default=options.get(CONF_RELOCK_DELAY, DEFAULT_RELOCK_DELAY),
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_RELOCK_DELAY,
                            max=MAX_RELOCK_DELAY,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Required(
                    CONF_ENABLE_WEBHOOK,
                    default=options.get(CONF_ENABLE_WEBHOOK, False),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"webhook_url": self._webhook_url()},
        )

    def _webhook_url(self) -> str:
        """Describe the URL ButterflyMX would be told to push to.

        Showing the real thing lets somebody check it is reachable before
        turning push on, instead of finding out by missing a visitor.
        """
        try:
            base = get_url(self.hass, allow_internal=False, prefer_external=True)
        except NoURLAvailableError:
            return (
                "unavailable - Home Assistant has no external URL configured, "
                "so ButterflyMX would have nowhere to push to"
            )

        webhook_id = self.config_entry.data.get(CONF_WEBHOOK_ID)
        if not webhook_id:
            # Entries created before this was allocated up front.  Settle on one
            # now so the URL shown is the URL registered.
            webhook_id = webhook.async_generate_id()
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_WEBHOOK_ID: webhook_id},
            )
        return f"{base}{webhook.async_generate_path(webhook_id)}"
