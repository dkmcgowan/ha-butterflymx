"""Shared fixtures for the ButterflyMX tests."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    CONF_ACCOUNTS_URL,
    CONF_API_URL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REDIRECT_URI,
    CONF_TOKEN,
    DOMAIN,
    ENV_PRODUCTION,
    OOB_REDIRECT_URI,
)

ACCOUNTS_URL = "https://accounts.butterflymx.com"
API_URL = "https://api.butterflymx.com"

TENANT_ID = 4242
BUILDING_ID = 777
UNIT_ID = 99
ACCESS_POINT_ID = 1001
SMART_LOCK_DEVICE_ID = 2002


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    return enable_custom_integrations


def make_token(expires_in: int = 86400, suffix: str = "1") -> dict[str, Any]:
    """Build a token payload as ButterflyMX would return it."""
    return {
        "access_token": f"access-{suffix}",
        "refresh_token": f"refresh-{suffix}",
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": "public",
        "created_at": int(time.time()),
        "expires_at": time.time() + expires_in,
    }


TENANTS_RESPONSE = {
    "data": [
        {
            "id": TENANT_ID,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "building_id": BUILDING_ID,
            "building_name": "Crimson",
            "building_timezone": "America/New_York",
            "unit": {"id": UNIT_ID, "label": "4B", "floor": "4"},
        }
    ],
    "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
}

ACCESS_POINTS_RESPONSE = {
    "data": [
        {
            "id": ACCESS_POINT_ID,
            "name": "Front Entrance",
            "building_id": BUILDING_ID,
            "device_ids": [5005],
            "open_hours": [],
        }
    ],
    "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
}

DEVICES_RESPONSE = {
    "data": [
        {
            "id": 5005,
            "name": "Lobby Panel",
            "type": "panel",
            "building_id": BUILDING_ID,
            "model": "BMX-P1",
            "serial_number": "SN-1",
        },
        {
            "id": SMART_LOCK_DEVICE_ID,
            "name": "Apartment Door",
            "type": "smart_lock",
            "building_id": BUILDING_ID,
            "model": "Yale",
            "serial_number": "SN-2",
        },
    ],
    "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
}

EMPTY_CALLS_RESPONSE: dict[str, Any] = {
    "data": [],
    "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
}

EMPTY_ACCESS_LOGS_RESPONSE: dict[str, Any] = {
    "data": [],
    "page_info": {"current_page": 1, "total_pages": 1, "next_page": None},
}


def access_log_payload(
    entry_id: int = 2170896283,
    logged_at: str = "2026-08-04T12:00:00Z",
    entry_method: Any = "App call",
) -> dict[str, Any]:
    """One door release, shaped the way the live API returns them."""
    return {
        "id": entry_id,
        "logged_at": logged_at,
        "access_point": ACCESS_POINT_ID,
        "release_status": "Unlocked",
        "release_type": "Tenant",
        "entry_method": entry_method,
        "name": "Ada Lovelace",
        "unit": UNIT_ID,
        "tenant_id": TENANT_ID,
        "image_url": "https://bmx-rails-production.s3.amazonaws.com/cache/x.jpeg",
    }


def call_payload(call_id: int = 900001, logged_at: str = "2026-08-04T12:00:00Z") -> dict[str, Any]:
    """Build a single call log entry."""
    return {
        "id": call_id,
        "building_id": BUILDING_ID,
        "logged_at": logged_at,
        "notification_type": "visitor",
        "recipient": {"id": TENANT_ID, "type": "Tenant"},
        "unit": {"id": UNIT_ID},
        "device": {"id": 5005, "name": "Lobby Panel"},
        "status": "initializing",
        "image_url": "https://cdn.example.com/snap.png",
    }


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured ButterflyMX config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="ButterflyMX Crimson 4B",
        unique_id="ada@example.com",
        data={
            CONF_ENVIRONMENT: ENV_PRODUCTION,
            CONF_ACCOUNTS_URL: ACCOUNTS_URL,
            CONF_API_URL: API_URL,
            CONF_CLIENT_ID: "client-id",
            CONF_CLIENT_SECRET: "client-secret",
            CONF_REDIRECT_URI: OOB_REDIRECT_URI,
            CONF_TOKEN: make_token(),
        },
        options={},
    )


@pytest.fixture
def mock_topology(aioclient_mock):
    """Mock the topology, and empty call and access logs."""
    aioclient_mock.get(f"{API_URL}/v4/tenants", json=TENANTS_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/access_points", json=ACCESS_POINTS_RESPONSE)
    aioclient_mock.get(f"{API_URL}/v4/devices", json=DEVICES_RESPONSE)
    aioclient_mock.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/calls", json=EMPTY_CALLS_RESPONSE
    )
    aioclient_mock.get(
        f"{API_URL}/v4/buildings/{BUILDING_ID}/access_logs",
        json=EMPTY_ACCESS_LOGS_RESPONSE,
    )
    return aioclient_mock
