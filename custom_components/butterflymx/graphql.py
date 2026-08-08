"""The GraphQL half of the ButterflyMX API.

Kept apart from ``api.py`` because it is a genuinely different API rather than
another path on the same one.  It has its own root query, its own connection
and pagination shape, its own way of numbering things, and its own habit of
reporting failure inside an HTTP 200.  None of that belongs mixed in with REST
resources that page with ``page_info.next_page`` and say what went wrong in the
status code.

What is *not* here is the transport.  Authorization, token refresh, retries and
backoff live once in :class:`~.api.ButterflyMXClient`, and this module is only
the query text and how to read the answer.  So the split is by knowledge, not
by connection: ``api.py`` knows how to make a request, this knows what to ask
and what came back.

Only one query so far.  See ``GRAPHQL_PATH`` in ``const.py`` for why it is
worth talking GraphQL at all, which comes down to ``openDuration`` existing
nowhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from .exceptions import ButterflyMXResponseError
from .models import AccessPointDetail

_LOGGER = logging.getLogger(__name__)

# Deliberately the smallest query that answers the question.  ``legacyId`` is
# the access point ID the rest of the integration already uses, so it is what
# the result is keyed by; the other three fields describe the door.  No
# building, no devices, no schedules: this runs on every topology refresh and
# should stay cheap.
#
# The root ``tenants`` connection takes no arguments, so nothing has to be
# looked up before this can be asked.
ACCESS_POINT_DETAIL_QUERY = """
query {
  tenants {
    pageInfo { hasNextPage }
    nodes {
      accessPoints {
        pageInfo { hasNextPage }
        nodes { legacyId name openDuration online inOpenHours }
      }
    }
  }
}
"""


def parse_access_point_details(payload: Any) -> dict[int, AccessPointDetail]:
    """Read a query result into door configuration, keyed by access point ID.

    Raises :exc:`ButterflyMXResponseError` if the query did not run.  An empty
    result is not an error: an account really can have no doors, and the caller
    treats a missing duration as "fall back" either way.
    """
    if not isinstance(payload, dict):
        raise ButterflyMXResponseError(
            "ButterflyMX returned a non-object body for the access point query"
        )

    # GraphQL reports its own failures inside a 200, so a successful request
    # does not mean a successful query.
    if errors := payload.get("errors"):
        raise ButterflyMXResponseError(
            f"ButterflyMX rejected the access point query: {_describe(errors)}"
        )

    data = payload.get("data")
    tenants = data.get("tenants") if isinstance(data, dict) else None
    if not isinstance(tenants, dict):
        _LOGGER.debug("No tenants in the access point query result: %s", payload)
        return {}

    details: dict[int, AccessPointDetail] = {}
    truncated = _has_another_page(tenants)

    for tenant in tenants.get("nodes") or []:
        if not isinstance(tenant, dict):
            continue
        access_points = tenant.get("accessPoints")
        if not isinstance(access_points, dict):
            continue
        truncated = truncated or _has_another_page(access_points)
        for node in access_points.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            detail = AccessPointDetail.from_graphql(node)
            if detail is not None:
                details[detail.access_point_id] = detail

    if truncated:
        # Never let a partial read look like a complete one.  The doors that did
        # arrive are still worth having; the rest keep their fallback.
        _LOGGER.warning(
            "ButterflyMX has more access points than one page returned, so %d "
            "door(s) know how long they stay open and any others fall back to "
            "a fixed guess",
            len(details),
        )

    return details


def _has_another_page(connection: Any) -> bool:
    """Report whether a GraphQL connection had more pages we did not read."""
    if not isinstance(connection, dict):
        return False
    page_info = connection.get("pageInfo")
    return bool(isinstance(page_info, dict) and page_info.get("hasNextPage"))


def _describe(errors: Any) -> str:
    """Summarize a GraphQL errors array for a log line."""
    if isinstance(errors, list):
        messages = [
            str(error.get("message"))
            for error in errors
            if isinstance(error, dict) and error.get("message")
        ]
        if messages:
            return ", ".join(messages)
    return str(errors)
