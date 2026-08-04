"""Tests for the ButterflyMX config flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.config_flow import extract_code
from custom_components.butterflymx.const import (
    CONF_ACCOUNTS_URL,
    CONF_API_URL,
    CONF_AUTH_CODE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REDIRECT_URI,
    CONF_TOKEN,
    DOMAIN,
    ENV_CUSTOM,
    ENV_SANDBOX,
    OOB_REDIRECT_URI,
)

from .conftest import ACCOUNTS_URL, API_URL, TENANTS_RESPONSE

TOKEN_URL = f"{ACCOUNTS_URL}/oauth/token"

TOKEN_RESPONSE = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "token_type": "Bearer",
    "expires_in": 86400,
    "scope": "public",
}

CREDENTIALS = {
    CONF_CLIENT_ID: "client-id",
    CONF_CLIENT_SECRET: "client-secret",
    CONF_REDIRECT_URI: OOB_REDIRECT_URI,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc123", "abc123"),
        ("  abc123  ", "abc123"),
        ("https://my.home-assistant.io/redirect/oauth?code=xyz&state=1", "xyz"),
        ("urn:ietf:wg:oauth:2.0:oob?code=q1", "q1"),
    ],
)
def test_extract_code(raw: str, expected: str) -> None:
    """Both a bare code and a full redirect URL are accepted."""
    assert extract_code(raw) == expected


async def _start_flow(hass: HomeAssistant, environment: str = ENV_SANDBOX):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENVIRONMENT: environment}
    )


async def test_full_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """The happy path links an account and creates an entry."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)

    result = await _start_flow(hass)
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "authorize"
    assert "authorize_url" in result["description_placeholders"]
    assert result["description_placeholders"]["authorize_url"].startswith(
        f"{ACCOUNTS_URL}/oauth/authorize?"
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "the-code"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ButterflyMX Crimson 4B"
    assert result["result"].unique_id == "ada@example.com"
    assert result["data"][CONF_API_URL] == API_URL
    assert result["data"][CONF_TOKEN]["access_token"] == "access-1"
    assert result["data"][CONF_TOKEN]["expires_at"] > 0


async def test_flow_accepts_pasted_redirect_url(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Users with a custom redirect URI can paste the whole URL."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_AUTH_CODE: "https://example.org/cb?code=pasted-code&state=1"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    token_request = aioclient_mock.mock_calls[0][2]
    assert token_request["code"] == "pasted-code"


async def test_flow_invalid_code(hass: HomeAssistant, aioclient_mock) -> None:
    """A rejected code re-shows the authorize step with an error."""
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "bad"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    """A 5xx from the accounts host is reported as a connection error."""
    aioclient_mock.post(TOKEN_URL, status=503, text="down")

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "code"}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_custom_environment(hass: HomeAssistant, aioclient_mock) -> None:
    """A dedicated deployment can supply its own hostnames."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)

    result = await _start_flow(hass, ENV_CUSTOM)
    assert result["step_id"] == "endpoints"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNTS_URL: "not-a-url", CONF_API_URL: API_URL}
    )
    assert result["errors"] == {"base": "invalid_url"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNTS_URL: ACCOUNTS_URL, CONF_API_URL: API_URL}
    )
    assert result["step_id"] == "credentials"


async def test_duplicate_account_aborts(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Linking the same ButterflyMX account twice is refused."""
    config_entry.add_to_hass(hass)
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "code"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(
    hass: HomeAssistant, mock_topology, config_entry: MockConfigEntry
) -> None:
    """Re-linking replaces the stored token on the existing entry."""
    config_entry.add_to_hass(hass)
    mock_topology.post(
        TOKEN_URL,
        json={**TOKEN_RESPONSE, "access_token": "access-9", "refresh_token": "refresh-9"},
    )

    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "code"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert config_entry.data[CONF_TOKEN]["access_token"] == "access-9"
