"""Tests for the ButterflyMX config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.config_flow import extract_code
from custom_components.butterflymx.const import (
    CONF_API_URL,
    CONF_AUTH_CODE,
    CONF_CALL_SCAN_INTERVAL,
    CONF_CLIENT_ID,
    CONF_ENABLE_WEBHOOK,
    CONF_REDIRECT_URI,
    CONF_RELOCK_DELAY,
    CONF_TOKEN,
    DOMAIN,
    OOB_REDIRECT_URI,
)

from .conftest import (
    ACCOUNTS_URL,
    API_URL,
    BUILDING_ID,
    TENANTS_RESPONSE,
    UNIT_ID,
)

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
    CONF_REDIRECT_URI: OOB_REDIRECT_URI,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc123", "abc123"),
        ("  abc123  ", "abc123"),
        ("https://my.home-assistant.io/redirect/oauth?code=xyz&state=1", "xyz"),
        ("urn:ietf:wg:oauth:2.0:oob?code=q1", "q1"),
        ("https://example.com/cb#code=frag9", "frag9"),
        # urlparse reads a lone query string as a path, so this needs its own pass.
        ("code=bare42", "bare42"),
        # URL-shaped but carrying no code: empty, so the user is told the paste
        # was wrong rather than that ButterflyMX rejected their credentials.
        ("https://example.com/redirect?state=1", ""),
        ("https://example.com/redirect", ""),
    ],
)
def test_extract_code(raw: str, expected: str) -> None:
    """Both a bare code and a full redirect URL are accepted."""
    assert extract_code(raw) == expected


async def _start_flow(hass: HomeAssistant):
    """Start setup.

    There is no environment step: production is the only one offered. With no
    client ID shipped, setup opens on the credentials form.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_a_shipped_client_id_skips_straight_to_signing_in(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """With a client ID shipped there is nothing to ask for.

    Setup becomes a single step: sign in and paste the code back. Nobody has to
    apply to a developer programme first.
    """
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)

    with patch(
        "custom_components.butterflymx.config_flow.DEFAULT_CLIENT_ID", "shipped-id"
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "authorize"
        assert "client_id=shipped-id" in result["description_placeholders"][
            "authorize_url"
        ]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTH_CODE: "the-code"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLIENT_ID] == "shipped-id"


async def test_setup_never_offers_sandbox(hass: HomeAssistant) -> None:
    """Sandbox is for development and must not be reachable from the UI."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["step_id"] == "credentials"
    assert "environment" not in (result.get("data_schema").schema or {})


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


def _tenants_without_email(*ids: int) -> dict:
    """Tenancies in the given order, with no email to fall back on."""
    return {
        "data": [
            {
                "id": tenant_id,
                "full_name": "Ada Lovelace",
                "building_id": BUILDING_ID,
                "building_name": "Crimson",
                "unit": {"id": UNIT_ID, "label": "4B"},
            }
            for tenant_id in ids
        ],
        "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
    }


@pytest.mark.parametrize("order", [(910, 920), (920, 910)])
async def test_account_id_does_not_depend_on_tenancy_order(
    hass: HomeAssistant, aioclient_mock, order: tuple[int, int]
) -> None:
    """The unique ID has to survive a reauth, whatever order the API replies in.

    With no email to key on it falls back to a tenancy ID, and an account can
    hold several. Picking whichever came first would rename the account when
    the order changed, and a reauth would then refuse the user's own
    credentials as belonging to somebody else.
    """
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=_tenants_without_email(*order))

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTH_CODE: "the-code"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "910"


async def test_options_are_stored_as_whole_seconds(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Number selectors hand back floats; these options are whole seconds."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CALL_SCAN_INTERVAL: 15,
            CONF_RELOCK_DELAY: 7,
            CONF_ENABLE_WEBHOOK: False,
        },
    )

    options = result["data"]
    assert options[CONF_CALL_SCAN_INTERVAL] == 15
    assert isinstance(options[CONF_CALL_SCAN_INTERVAL], int)
    assert options[CONF_RELOCK_DELAY] == 7
    assert isinstance(options[CONF_RELOCK_DELAY], int)


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
