"""Tests for the ButterflyMX topology coordinator."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.butterflymx.coordinator import ButterflyMXTopologyCoordinator
from custom_components.butterflymx.exceptions import ButterflyMXConnectionError
from custom_components.butterflymx.models import AccessPoint, Tenant

BUILDINGS = (1, 2, 3)


class StubClient:
    """Client whose access-point lookups fail for the named buildings."""

    def __init__(self, unreachable: set[int]) -> None:
        """Record which buildings should fail."""
        self.unreachable = unreachable

    async def async_get_tenants(self) -> list[Tenant]:
        return [Tenant(id=10 + b, building_id=b) for b in BUILDINGS]

    async def async_get_access_points(self, building_id: int) -> list[AccessPoint]:
        if building_id in self.unreachable:
            raise ButterflyMXConnectionError(f"building {building_id} is unreachable")
        return [
            AccessPoint(
                id=100 + building_id, building_id=building_id, name=f"Door {building_id}"
            )
        ]

    async def async_get_devices(self, building_id: int) -> list:
        return []

    async def async_get_access_tools(self) -> list:
        return []

    async def async_get_access_point_details(self) -> dict:
        return {}


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, unreachable: set[int]
) -> ButterflyMXTopologyCoordinator:
    return ButterflyMXTopologyCoordinator(hass, entry, StubClient(unreachable))


@pytest.mark.parametrize(
    ("unreachable", "expected"),
    [(set(), [1, 2, 3]), ({2}, [1, 3]), ({1, 3}, [2])],
)
async def test_a_failing_building_does_not_affect_the_others(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    unreachable: set[int],
    expected: list[int],
) -> None:
    """An account can span buildings, and one going down must not lose the rest."""
    topology = await _coordinator(hass, config_entry, unreachable)._async_update_data()

    assert sorted(ap.building_id for ap in topology.access_points) == expected
    # Tenancies come from a single call, so they survive regardless.
    assert len(topology.tenants) == len(BUILDINGS)


async def test_every_building_failing_fails_the_refresh(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Losing every building is a general failure, not one bad building.

    Failing the refresh keeps the previous topology, rather than reporting that
    every door in the account has disappeared.
    """
    coordinator = _coordinator(hass, config_entry, set(BUILDINGS))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_failing_building_is_reported(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A building whose doors went missing says so in the log."""
    await _coordinator(hass, config_entry, {2})._async_update_data()

    assert "building 2" in caplog.text
    assert "other buildings are unaffected" in caplog.text
