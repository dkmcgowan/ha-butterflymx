"""Tests for visitor and delivery passes.

The thread running through most of these is that a pass carries a working door
code, so the interesting question is usually not "did it work" but "where did
the code end up".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.const import (
    DOMAIN,
    SERVICE_CREATE_DELIVERY_PASS,
    SERVICE_CREATE_VISITOR_PASS,
    SERVICE_LIST_PASSES,
    SERVICE_REVOKE_PASS,
)
from custom_components.butterflymx.models import Keychain, Pass, VirtualKey

from .conftest import (
    ACCESS_POINT_ID,
    API_URL,
    KEYCHAIN_ID,
    TENANT_ID,
    VIRTUAL_KEY_ID,
    keychain_payload,
    register_topology,
    virtual_key_payload,
)

PASSES_ENTITY = "sensor.unit_4b_passes"
LOCK_ENTITY = "lock.front_entrance"

PIN = "906613"


async def setup_with_passes(
    hass: HomeAssistant,
    aioclient_mock,
    config_entry: MockConfigEntry,
    *keychains: dict,
    virtual_keys: list[dict] | None = None,
):
    """Set the integration up with a given set of passes on the account.

    The pass payloads are passed in rather than registered afterwards because
    the mocker matches in registration order, so a second registration for the
    same URL never takes effect.
    """
    mock = register_topology(
        aioclient_mock,
        keychains=list(keychains),
        virtual_keys=[virtual_key_payload()] if virtual_keys is None else virtual_keys,
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return mock


def request_body(mock, suffix: str) -> dict:
    """Return the body of the last request sent to a path."""
    return next(
        call[2] for call in reversed(mock.mock_calls) if str(call[1]).endswith(suffix)
    )


def was_deleted(mock, suffix: str) -> bool:
    """Report whether a DELETE was sent to a path.

    The recorded method is whatever the client passed, which is uppercase.
    """
    return any(
        call[0].lower() == "delete" and str(call[1]).endswith(suffix)
        for call in mock.mock_calls
    )


async def call_service(hass: HomeAssistant, service: str, **data):
    """Call a pass service against the passes sensor and unwrap the response.

    Home Assistant keys an entity service's response by entity ID, the same
    way weather.get_forecasts does, so there is always exactly one entry here.
    """
    response = await hass.services.async_call(
        DOMAIN,
        service,
        {"entity_id": PASSES_ENTITY, **data},
        blocking=True,
        return_response=True,
    )
    return response[PASSES_ENTITY]


# --- Parsing -----------------------------------------------------------------


def test_a_keychain_is_parsed_with_its_doors_and_window() -> None:
    """The fields the services and the sensor rely on all survive parsing."""
    keychain = Keychain.from_api(keychain_payload())

    assert keychain is not None
    assert keychain.id == KEYCHAIN_ID
    assert keychain.type == "custom_keychain"
    assert keychain.access_point_ids == (ACCESS_POINT_ID,)
    assert keychain.virtual_key_ids == (VIRTUAL_KEY_ID,)
    assert keychain.tenant_id == TENANT_ID


def test_a_keychain_with_no_id_is_dropped() -> None:
    """Without an ID it cannot be revoked, so it is not worth keeping."""
    payload = keychain_payload()
    del payload["id"]

    assert Keychain.from_api(payload) is None


def test_a_pass_with_no_end_date_never_expires() -> None:
    """A missing bound means unbounded, not immediately invalid."""
    keychain = Keychain.from_api(keychain_payload(ends_at=None))

    assert keychain is not None
    assert keychain.is_active(datetime(2099, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("when", "active"),
    [
        ("2026-08-07T11:59:00Z", False),
        ("2026-08-07T14:00:00Z", True),
        ("2026-08-07T16:01:00Z", False),
    ],
    ids=["before", "during", "after"],
)
def test_a_pass_is_active_only_inside_its_window(when: str, active: bool) -> None:
    """Both bounds are honored."""
    keychain = Keychain.from_api(keychain_payload())

    assert keychain is not None
    assert keychain.is_active(dt_util.parse_datetime(when)) is active


def test_the_summary_carries_no_credentials() -> None:
    """The one property everything user-visible depends on.

    ``as_summary`` is what reaches entity attributes, and from there recorder,
    the logbook and diagnostics. A PIN must never come along.
    """
    key = VirtualKey.from_api(virtual_key_payload())
    keychain = Keychain.from_api(keychain_payload())
    assert key is not None and keychain is not None

    summary = json.dumps(Pass(keychain=keychain, keys=(key,)).as_summary())

    assert PIN not in summary
    assert "qr_code" not in summary
    assert "instructions_url" not in summary
    assert "pin_code" not in summary


def test_the_response_does_carry_the_credentials() -> None:
    """The counterpart: a service response is where the code is allowed to go."""
    key = VirtualKey.from_api(virtual_key_payload())
    keychain = Keychain.from_api(keychain_payload())
    assert key is not None and keychain is not None

    response = Pass(keychain=keychain, keys=(key,)).as_response()

    assert response["keys"][0]["pin_code"] == PIN
    assert response["keys"][0]["qr_code_url"]


# --- Sensor ------------------------------------------------------------------


async def test_the_sensor_counts_active_passes_and_hides_the_codes(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """State is the count; attributes describe the passes but not their codes."""
    freezer.move_to("2026-08-07T14:00:00Z")
    await setup_with_passes(hass, aioclient_mock, config_entry, keychain_payload())

    state = hass.states.get(PASSES_ENTITY)
    assert state is not None
    assert state.state == "1"

    passes = state.attributes["passes"]
    assert len(passes) == 1
    assert passes[0]["pass_id"] == KEYCHAIN_ID
    assert passes[0]["name"] == "Cleaner"
    assert passes[0]["used"] is False
    assert PIN not in json.dumps(dict(state.attributes))


async def test_an_expired_pass_is_listed_but_not_counted(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """It still exists on the account, so it stays visible to be tidied up."""
    freezer.move_to("2026-08-08T14:00:00Z")
    await setup_with_passes(hass, aioclient_mock, config_entry, keychain_payload())

    state = hass.states.get(PASSES_ENTITY)
    assert state is not None
    assert state.state == "0"
    assert len(state.attributes["passes"]) == 1


async def test_a_used_pass_says_so(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """Usage is the one thing worth automating on, so it is an attribute."""
    freezer.move_to("2026-08-07T14:00:00Z")
    await setup_with_passes(
        hass,
        aioclient_mock,
        config_entry,
        keychain_payload(),
        virtual_keys=[virtual_key_payload(usage_count=2)],
    )

    state = hass.states.get(PASSES_ENTITY)
    assert state is not None
    assert state.attributes["passes"][0]["used"] is True
    assert state.attributes["passes"][0]["usage_count"] == 2


# --- Services ----------------------------------------------------------------


async def test_creating_a_delivery_pass_returns_the_code(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """The PIN comes back to the caller, which is the only place it goes."""
    mock = await setup_with_passes(hass, aioclient_mock, config_entry)
    mock.post(
        f"{API_URL}/v4/keychains/delivery_pass",
        json={"data": keychain_payload(name="Amazon", kind="delivery_pass")},
        status=201,
    )

    response = await call_service(hass, SERVICE_CREATE_DELIVERY_PASS, name="Amazon")

    assert response["pass_id"] == KEYCHAIN_ID
    assert response["keys"][0]["pin_code"] == PIN
    assert request_body(mock, "keychains/delivery_pass") == {
        "keychain": {"name": "Amazon", "tenant_id": TENANT_ID}
    }


async def test_creating_a_visitor_pass_issues_its_own_code(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A custom keychain has no key on it, so one has to be created for it."""
    mock = await setup_with_passes(hass, aioclient_mock, config_entry)
    mock.post(
        f"{API_URL}/v4/keychains/custom",
        json={"data": keychain_payload(virtual_key_ids=[])},
        status=201,
    )
    mock.post(
        f"{API_URL}/v4/virtual_keys", json={"data": virtual_key_payload()}, status=201
    )

    response = await call_service(
        hass,
        SERVICE_CREATE_VISITOR_PASS,
        name="Cleaner",
        starts_at="2026-08-07T12:00:00+00:00",
        ends_at="2026-08-07T16:00:00+00:00",
    )

    assert response["keys"][0]["pin_code"] == PIN
    assert request_body(mock, "virtual_keys") == {
        "virtual_key": {"keychain_id": KEYCHAIN_ID, "name": "Cleaner"}
    }


async def test_a_visitor_pass_with_no_window_gets_a_sensible_one(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry, freezer
) -> None:
    """Both bounds are optional: now, for a few hours."""
    freezer.move_to("2026-08-07T12:00:00Z")
    mock = await setup_with_passes(hass, aioclient_mock, config_entry)
    mock.post(
        f"{API_URL}/v4/keychains/custom",
        json={"data": keychain_payload(virtual_key_ids=[])},
        status=201,
    )
    mock.post(
        f"{API_URL}/v4/virtual_keys", json={"data": virtual_key_payload()}, status=201
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_VISITOR_PASS,
        {"entity_id": PASSES_ENTITY, "name": "Cleaner"},
        blocking=True,
        return_response=True,
    )

    body = request_body(mock, "keychains/custom")["keychain"]
    starts = datetime.fromisoformat(body["starts_at"])
    ends = datetime.fromisoformat(body["ends_at"])
    assert ends - starts == timedelta(hours=4)


async def test_a_backwards_window_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Caught here rather than sent, so the message says what is wrong."""
    await setup_with_passes(hass, aioclient_mock, config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_VISITOR_PASS,
            {
                "entity_id": PASSES_ENTITY,
                "name": "Cleaner",
                "starts_at": "2026-08-07T16:00:00+00:00",
                "ends_at": "2026-08-07T12:00:00+00:00",
            },
            blocking=True,
            return_response=True,
        )


async def test_doors_are_chosen_as_lock_entities(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Users pick doors the way they see them; IDs are worked out from there."""
    mock = await setup_with_passes(hass, aioclient_mock, config_entry)
    assert hass.states.get(LOCK_ENTITY) is not None

    mock.post(
        f"{API_URL}/v4/keychains/custom",
        json={"data": keychain_payload(virtual_key_ids=[])},
        status=201,
    )
    mock.post(
        f"{API_URL}/v4/virtual_keys", json={"data": virtual_key_payload()}, status=201
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_VISITOR_PASS,
        {"entity_id": PASSES_ENTITY, "name": "Cleaner", "doors": [LOCK_ENTITY]},
        blocking=True,
        return_response=True,
    )

    body = request_body(mock, "keychains/custom")["keychain"]
    assert body["access_point_ids"] == [ACCESS_POINT_ID]


async def test_a_door_from_another_integration_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Silently ignoring it would grant every door instead of the one asked for."""
    await setup_with_passes(hass, aioclient_mock, config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_VISITOR_PASS,
            {
                "entity_id": PASSES_ENTITY,
                "name": "Cleaner",
                "doors": ["lock.somebody_elses_door"],
            },
            blocking=True,
            return_response=True,
        )


async def test_a_pass_whose_code_cannot_be_issued_is_taken_back(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A keychain with no key lets nobody in, so it is not left lying around."""
    mock = await setup_with_passes(hass, aioclient_mock, config_entry)
    mock.post(
        f"{API_URL}/v4/keychains/custom",
        json={"data": keychain_payload(virtual_key_ids=[])},
        status=201,
    )
    mock.post(f"{API_URL}/v4/virtual_keys", status=500)
    mock.delete(f"{API_URL}/v4/keychains/{KEYCHAIN_ID}", status=204)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_VISITOR_PASS,
            {"entity_id": PASSES_ENTITY, "name": "Cleaner"},
            blocking=True,
            return_response=True,
        )

    assert was_deleted(mock, f"keychains/{KEYCHAIN_ID}")


async def test_listing_passes_returns_the_codes(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """How a code is read back off a pass created days ago."""
    await setup_with_passes(hass, aioclient_mock, config_entry, keychain_payload())

    response = await call_service(hass, SERVICE_LIST_PASSES)

    assert len(response["passes"]) == 1
    assert response["passes"][0]["keys"][0]["pin_code"] == PIN


async def test_revoking_a_pass_deletes_the_keychain(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Deleting the keychain takes its codes with it, so that is all it takes."""
    mock = await setup_with_passes(
        hass, aioclient_mock, config_entry, keychain_payload()
    )
    mock.delete(f"{API_URL}/v4/keychains/{KEYCHAIN_ID}", status=204)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REVOKE_PASS,
        {"entity_id": PASSES_ENTITY, "pass_id": KEYCHAIN_ID},
        blocking=True,
    )

    assert was_deleted(mock, f"keychains/{KEYCHAIN_ID}")


async def test_revoking_an_unknown_pass_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """A stale ID would otherwise delete somebody else's pass without asking."""
    await setup_with_passes(hass, aioclient_mock, config_entry, keychain_payload())

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REVOKE_PASS,
            {"entity_id": PASSES_ENTITY, "pass_id": 1},
            blocking=True,
        )


async def test_the_wrong_sensor_is_refused(
    hass: HomeAssistant, aioclient_mock, config_entry: MockConfigEntry
) -> None:
    """Every sensor on the platform receives the call, so the target is checked."""
    await setup_with_passes(hass, aioclient_mock, config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_DELIVERY_PASS,
            {"entity_id": "sensor.unit_4b_last_call", "name": "Amazon"},
            blocking=True,
            return_response=True,
        )
