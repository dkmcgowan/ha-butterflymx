"""Exceptions raised by the ButterflyMX integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class ButterflyMXError(HomeAssistantError):
    """Base error for all ButterflyMX failures."""


class ButterflyMXConnectionError(ButterflyMXError):
    """Raised when the ButterflyMX cloud could not be reached."""


class ButterflyMXAuthError(ButterflyMXError):
    """Raised when credentials are rejected and re-authorization is required."""


class ButterflyMXRateLimitError(ButterflyMXError):
    """Raised when ButterflyMX asks us to slow down and retries are exhausted."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record how long the server asked us to wait."""
        super().__init__(message)
        self.retry_after = retry_after


class ButterflyMXResponseError(ButterflyMXError):
    """Raised when the API returns an unexpected status or body."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Record the HTTP status that caused the failure."""
        super().__init__(message)
        self.status = status
