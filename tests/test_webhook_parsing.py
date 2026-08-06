"""Tests for the forgiving webhook payload parser."""

from __future__ import annotations

import pytest

from custom_components.butterflymx.webhook import parse_call_payload


def _documented(resource_type: str, data: dict) -> dict:
    """Wrap a body in the envelope ButterflyMX documents for a delivery."""
    return {
        "event": {"resource_type": resource_type, "action": "create", "data": data}
    }


def test_documented_event_envelope() -> None:
    """The shape the API documentation actually shows.

    Every other shape here is a guess made before the documentation was read.
    This is the one deliveries are expected to arrive in.
    """
    call = parse_call_payload(
        _documented(
            "call",
            {
                "id": 123456789,
                "logged_at": "2026-04-24T10:24:35Z",
                "image_url": "https://cdn.example/snapshot.png",
            },
        ),
        777,
    )

    assert call is not None
    assert call.id == 123456789
    assert call.building_id == 777
    assert call.logged_at is not None


def test_documented_door_release_is_not_a_doorbell() -> None:
    """A door opening is not somebody at the door."""
    payload = _documented(
        "door_release",
        {"id": 1, "access_point": 22177636, "name": "John Tenant"},
    )
    assert parse_call_payload(payload, 777) is None


def test_integration_events_are_not_calls() -> None:
    """A notification about a webhook registration must not ring the doorbell."""
    assert parse_call_payload(_documented("integrations", {"id": 1}), 777) is None


def test_bare_call_object() -> None:
    """A plain call object is accepted."""
    call = parse_call_payload({"id": 5, "building_id": 7, "unit": {"id": 9}})
    assert call is not None and call.id == 5


def test_jsonapi_envelope() -> None:
    """A ``data.attributes`` envelope is unwrapped."""
    call = parse_call_payload(
        {
            "data": {
                "type": "call",
                "attributes": {
                    "id": 6,
                    "logged_at": "2026-08-04T12:00:00Z",
                    "unit": {"id": 9},
                },
            }
        },
        7,
    )
    assert call is not None
    assert call.id == 6
    assert call.building_id == 7


def test_resource_wrapper() -> None:
    """A ``resource_type``/``resource`` wrapper is unwrapped."""
    call = parse_call_payload(
        {"resource_type": "call", "resource": {"id": 7, "building_id": 7}}
    )
    assert call is not None and call.id == 7


def test_door_release_events_are_ignored() -> None:
    """Only call events drive the doorbell."""
    assert parse_call_payload({"resource_type": "door_release", "data": {"id": 8}}, 7) is None


@pytest.mark.parametrize("payload", ["nope", None, 5, [], {}])
def test_junk_is_ignored(payload: object) -> None:
    """Anything unparseable is dropped rather than raising."""
    assert parse_call_payload(payload) is None


def test_missing_building_without_default_is_dropped() -> None:
    """Without a building we cannot attribute the call."""
    assert parse_call_payload({"id": 5}) is None
