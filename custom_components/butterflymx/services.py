"""Services for issuing and revoking ButterflyMX passes.

A pass is a code someone else can use to get in: a *delivery pass* is single
use, a *visitor pass* is reusable inside a window.  Those are the names the
ButterflyMX app uses, so anyone who has issued one on their phone already knows
what these do.  Underneath, both are a keychain -- the grant -- plus a virtual
key -- the credential -- but that split is ButterflyMX's plumbing and not
something a Home Assistant user should have to hold in their head.

These are entity services on the passes sensor.  An account can hold more than
one tenancy, and every pass belongs to exactly one, so targeting the sensor is
what says which unit a pass is for.  With a single unit the target picker offers
one thing and the question does not arise.

**The codes come back in the service response, not in entity state.**  A PIN and
a QR link open a real door.  Putting them in an attribute would write them to
the state machine, then to recorder, where they would sit for weeks in a
database that gets copied into backups; the logbook and any diagnostics upload
would pick them up on the way past.  A service response goes to the caller that
asked and is never stored, which is the difference between showing someone a
code and keeping a copy of it.  So ``create_*`` and ``list_passes`` hand back
the PIN and QR, and the sensor only ever says that a pass exists and whether it
has been used.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    ATTR_DOORS,
    ATTR_ENDS_AT,
    ATTR_PASS_ID,
    ATTR_PASSES,
    ATTR_STARTS_AT,
    DEFAULT_VISITOR_PASS_HOURS,
    DOMAIN,
    SERVICE_CREATE_DELIVERY_PASS,
    SERVICE_CREATE_VISITOR_PASS,
    SERVICE_LIST_PASSES,
    SERVICE_REVOKE_PASS,
)
from .exceptions import ButterflyMXError
from .models import Pass

if TYPE_CHECKING:
    from .sensor import ButterflyMXPassesSensor

_LOGGER = logging.getLogger(__name__)

CREATE_DELIVERY_PASS_SCHEMA = {
    vol.Required("name"): cv.string,
}

CREATE_VISITOR_PASS_SCHEMA = {
    vol.Required("name"): cv.string,
    vol.Optional(ATTR_STARTS_AT): cv.datetime,
    vol.Optional(ATTR_ENDS_AT): cv.datetime,
    vol.Optional(ATTR_DOORS): cv.entity_ids,
}

REVOKE_PASS_SCHEMA = {
    vol.Required(ATTR_PASS_ID): vol.Coerce(int),
}


def async_register_pass_services(platform: EntityPlatform) -> None:
    """Attach the pass services to the sensor platform."""
    platform.async_register_entity_service(
        SERVICE_CREATE_DELIVERY_PASS,
        CREATE_DELIVERY_PASS_SCHEMA,
        _async_create_delivery_pass,
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_CREATE_VISITOR_PASS,
        CREATE_VISITOR_PASS_SCHEMA,
        _async_create_visitor_pass,
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_LIST_PASSES,
        None,
        _async_list_passes,
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_REVOKE_PASS,
        REVOKE_PASS_SCHEMA,
        _async_revoke_pass,
    )


def _passes_sensor(entity: Entity) -> ButterflyMXPassesSensor:
    """Insist the target really is a passes sensor.

    An entity service reaches every entity on its platform, and this platform
    also has the last-call and last-door-opened sensors on it.  Home Assistant's
    target filters cannot narrow the picker past ``domain: sensor``, so picking
    one of those is easy to do and would otherwise fail deep inside the handler
    with an AttributeError.
    """
    # Imported here rather than at module scope: sensor.py imports this module
    # to register the services, so importing it back at the top would be a cycle.
    from .sensor import ButterflyMXPassesSensor as _PassesSensor

    if not isinstance(entity, _PassesSensor):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_passes_sensor",
            translation_placeholders={"entity": entity.entity_id},
        )
    return entity


async def _async_create_delivery_pass(
    entity: Entity, call: ServiceCall
) -> dict[str, Any]:
    """Create a single-use code for a delivery.

    Everything about it is ButterflyMX's decision: it opens every door, starts
    now, lasts 30 days and stops working once used.  A name is all there is to
    supply, and it is worth supplying a real one -- it is what the access log
    will show when the code is used.
    """
    entity = _passes_sensor(entity)
    client = entity.client
    try:
        keychain = await client.async_create_delivery_pass(
            entity.tenant.id, call.data["name"]
        )
        # A delivery pass issues its own key, but not synchronously enough to
        # come back in the create response, so read it separately.
        keys = await client.async_get_virtual_keys(keychain.id)
    except ButterflyMXError as err:
        raise HomeAssistantError(f"Could not create the delivery pass: {err}") from err

    return await _async_finish(entity, Pass(keychain=keychain, keys=tuple(keys)))


async def _async_create_visitor_pass(
    entity: Entity, call: ServiceCall
) -> dict[str, Any]:
    """Create a reusable code valid over a window.

    Unlike a delivery pass this takes two calls: a custom keychain comes back
    with no keys on it at all, so the credential has to be issued separately.
    Until that second call lands the pass exists but nothing can use it, which
    is why a failure there is reported rather than swallowed.
    """
    entity = _passes_sensor(entity)
    starts_at, ends_at = _window(call.data)
    doors = call.data.get(ATTR_DOORS)
    access_point_ids, device_ids = (
        _resolve_doors(entity, doors) if doors else ([], [])
    )

    client = entity.client
    try:
        keychain = await client.async_create_visitor_pass(
            entity.tenant.id,
            call.data["name"],
            starts_at,
            ends_at,
            access_point_ids=access_point_ids,
            device_ids=device_ids,
        )
    except ButterflyMXError as err:
        raise HomeAssistantError(f"Could not create the visitor pass: {err}") from err

    try:
        key = await client.async_create_virtual_key(keychain.id, call.data["name"])
    except ButterflyMXError as err:
        # The keychain exists and is useless without a key, so take it back out
        # rather than leaving a pass nobody can use lying on the account.
        await _async_discard(entity, keychain.id)
        raise HomeAssistantError(
            f"Created the visitor pass but could not issue its code, so the "
            f"pass was removed again: {err}"
        ) from err

    return await _async_finish(entity, Pass(keychain=keychain, keys=(key,)))


async def _async_list_passes(entity: Entity, call: ServiceCall) -> dict[str, Any]:
    """Return this unit's passes, codes included.

    The counterpart to keeping credentials out of entity state: the codes are
    still there whenever they are wanted, they just have to be asked for.  Run
    this from Developer tools -> Actions to read a code back off a pass created
    days ago.
    """
    entity = _passes_sensor(entity)
    await entity.coordinator.async_request_refresh()
    return {ATTR_PASSES: [record.as_response() for record in entity.passes]}


async def _async_revoke_pass(entity: Entity, call: ServiceCall) -> None:
    """Delete a pass and every code it issued."""
    entity = _passes_sensor(entity)
    pass_id = call.data[ATTR_PASS_ID]
    known = entity.passes
    if known and not any(record.keychain.id == pass_id for record in known):
        # Deleting somebody else's pass would still succeed, so check first.
        # The ID most likely came from this unit's sensor and is simply stale.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_pass",
            translation_placeholders={
                "pass_id": str(pass_id),
                "entity": entity.entity_id,
            },
        )

    try:
        await entity.client.async_delete_keychain(pass_id)
    except ButterflyMXError as err:
        raise HomeAssistantError(f"Could not revoke pass {pass_id}: {err}") from err

    await entity.coordinator.async_request_refresh()


async def _async_finish(
    entity: ButterflyMXPassesSensor, record: Pass
) -> dict[str, Any]:
    """Refresh the sensor and describe the new pass to the caller."""
    await entity.coordinator.async_request_refresh()
    return record.as_response()


async def _async_discard(entity: ButterflyMXPassesSensor, keychain_id: int) -> None:
    """Best-effort cleanup of a half-created pass."""
    try:
        await entity.client.async_delete_keychain(keychain_id)
    except ButterflyMXError as err:
        _LOGGER.warning(
            "Could not remove the incomplete pass %s; it has no code on it and "
            "will not let anyone in, but it will show up until it is deleted: %s",
            keychain_id,
            err,
        )


def _window(data: dict[str, Any]) -> tuple[datetime, datetime]:
    """Work out when a visitor pass starts and ends.

    Both bounds are optional: omitting the start means now, and omitting the end
    means a few hours from the start.  A datetime typed into the UI has no zone
    on it, so it is read as the Home Assistant time zone rather than as UTC --
    otherwise "let them in at 2pm" would silently mean something else.
    """
    now = dt_util.utcnow()
    starts_at = dt_util.as_utc(data[ATTR_STARTS_AT]) if ATTR_STARTS_AT in data else now
    if ATTR_ENDS_AT in data:
        ends_at = dt_util.as_utc(data[ATTR_ENDS_AT])
    else:
        ends_at = starts_at + timedelta(hours=DEFAULT_VISITOR_PASS_HOURS)

    if ends_at <= starts_at:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="pass_window_backwards",
            translation_placeholders={
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )
    return starts_at, ends_at


def _resolve_doors(
    entity: ButterflyMXPassesSensor, entity_ids: list[str]
) -> tuple[list[int], list[int]]:
    """Turn lock entities into the door IDs ButterflyMX wants.

    Users pick doors the way they see them, as the lock entities this
    integration already created.  Each one's unique ID says which kind of door
    is behind it, and that is the only translation needed.
    """
    registry = er.async_get(entity.hass)
    access_point_ids: list[int] = []
    device_ids: list[int] = []

    for entity_id in entity_ids:
        record = registry.async_get(entity_id)
        if record is None or record.platform != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_butterflymx_door",
                translation_placeholders={"entity": entity_id},
            )
        kind, _, raw_id = record.unique_id.removeprefix(f"{DOMAIN}_").rpartition("_")
        if kind == "access_point":
            access_point_ids.append(int(raw_id))
        elif kind == "device":
            device_ids.append(int(raw_id))
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_butterflymx_door",
                translation_placeholders={"entity": entity_id},
            )

    return access_point_ids, device_ids
