# SPDX-License-Identifier: MPL-2.0
"""Typed errors exposed by :mod:`samsarix_notifications`."""

from __future__ import annotations

from ._validation import MAX_RETRY_AFTER_SECONDS, is_bounded_number


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
    """Transport failure, optionally with a minimum retry delay of up to one day."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "delivery_failed",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        if retry_after_seconds is not None:
            if not is_bounded_number(retry_after_seconds, 0, MAX_RETRY_AFTER_SECONDS):
                raise NotificationValidationError("retry_after_seconds must be between 0 and 86400")
            if retryable is not True:
                raise NotificationValidationError(
                    "retry_after_seconds requires a retryable failure"
                )
        super().__init__(message, code=code, retryable=retryable)
        self.retry_after_seconds = retry_after_seconds


class OutboxConflictError(NotificationError):
    """Raised when durable state conflicts with an enqueue or operator action."""


class OutboxLeaseError(NotificationError):
    """Raised when a worker can no longer finalize its claimed message."""
