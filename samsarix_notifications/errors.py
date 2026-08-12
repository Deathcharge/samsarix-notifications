# SPDX-License-Identifier: MPL-2.0
"""Typed errors exposed by :mod:`samsarix_notifications`."""

from __future__ import annotations


class NotificationError(Exception):
    """Base exception with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class NotificationValidationError(NotificationError, ValueError):
    """Raised when a caller supplies an invalid notification value."""

    def __init__(self, message: str, *, code: str = "invalid_input") -> None:
        super().__init__(message, code=code, retryable=False)


class ConfigurationError(NotificationError):
    """Raised when a required transport setting is missing or contradictory."""

    def __init__(self, message: str, *, code: str = "invalid_configuration") -> None:
        super().__init__(message, code=code, retryable=False)


class DeliveryError(NotificationError):
    """Raised by a transport when an attempted delivery is not accepted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "delivery_failed",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class OutboxConflictError(NotificationError):
    """Raised when durable state conflicts with an enqueue or operator action."""


class OutboxLeaseError(NotificationError):
    """Raised when a worker can no longer finalize its claimed message."""
