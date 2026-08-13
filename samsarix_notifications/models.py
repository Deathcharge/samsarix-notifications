# SPDX-License-Identifier: MPL-2.0
"""Public data models for notification delivery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .errors import NotificationValidationError


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class NotificationChannel(str, Enum):
    """Known channel names.

    Email and webhook transports are included. The remaining names are retained
    for API compatibility and can be implemented by registering a custom
    transport with :class:`NotificationService`.
    """

    EMAIL = "email"
    WEBHOOK = "webhook"
    DISCORD = "discord"
    SLACK = "slack"
    SMS = "sms"
    PUSH = "push"


class DeliveryStatus(str, Enum):
    """Final status of a local delivery operation."""

    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(slots=True)
class NotificationPayload:
    """A validated notification request.

    ``max_retries`` is capped to prevent accidental cost amplification. An
    ``idempotency_key`` deduplicates successful deliveries within one service
    process and is recommended whenever callers may retry a request.
    """

    channel: NotificationChannel | str
    recipient: str
    subject: str
    body: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority: str = "normal"
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = field(default_factory=utc_now)
    idempotency_key: str | None = None
    notification_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        channel = (
            self.channel.value if isinstance(self.channel, NotificationChannel) else self.channel
        )
        if not isinstance(channel, str) or not channel.strip():
            raise NotificationValidationError("channel must be a non-empty string")
        self.channel = channel.strip().lower()

        self.recipient = _bounded_text("recipient", self.recipient, maximum=2_048)
        self.subject = _bounded_text("subject", self.subject, maximum=998)
        self.body = _bounded_text("body", self.body, maximum=1_000_000)
        self.notification_id = _bounded_text("notification_id", self.notification_id, maximum=128)

        if self.priority not in {"low", "normal", "high", "urgent"}:
            raise NotificationValidationError("priority must be one of: low, normal, high, urgent")
        if not isinstance(self.retry_count, int) or not 0 <= self.retry_count <= 10:
            raise NotificationValidationError("retry_count must be between 0 and 10")
        if not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 10:
            raise NotificationValidationError("max_retries must be between 0 and 10")
        if self.retry_count > self.max_retries:
            raise NotificationValidationError("retry_count cannot exceed max_retries")
        if self.idempotency_key is not None:
            self.idempotency_key = _bounded_text(
                "idempotency_key", self.idempotency_key, maximum=128
            )
        if not isinstance(self.metadata, Mapping):
            raise NotificationValidationError("metadata must be a mapping")
        self.metadata = dict(self.metadata)
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        else:
            self.created_at = self.created_at.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class TransportResult:
    """A transport's successful acknowledgement."""

    provider_id: str | None = None
    provider_status: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The truthful outcome returned for every notification operation."""

    notification_id: str
    channel: str
    recipient: str
    status: DeliveryStatus
    attempts: int
    started_at: datetime
    completed_at: datetime
    provider_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    deduplicated: bool = False
    retryable: bool | None = None

    @property
    def success(self) -> bool:
        """Whether a transport accepted the notification."""

        return self.status is DeliveryStatus.DELIVERED

    def __bool__(self) -> bool:
        """Preserve intuitive use in existing boolean checks."""

        return self.success

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation without exception objects."""

        return {
            "notification_id": self.notification_id,
            "channel": self.channel,
            "recipient": self.recipient,
            "status": self.status.value,
            "attempts": self.attempts,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "provider_id": self.provider_id,
            "provider_status": self.provider_status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "deduplicated": self.deduplicated,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry and timeout settings applied by the dispatcher."""

    max_retries: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0
    attempt_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 10:
            raise NotificationValidationError("max_retries must be between 0 and 10")
        if not 0 <= self.base_delay_seconds <= 60:
            raise NotificationValidationError("base_delay_seconds must be between 0 and 60")
        if not 0 <= self.max_delay_seconds <= 300:
            raise NotificationValidationError("max_delay_seconds must be between 0 and 300")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise NotificationValidationError(
                "max_delay_seconds cannot be less than base_delay_seconds"
            )
        if not 0 < self.attempt_timeout_seconds <= 300:
            raise NotificationValidationError("attempt_timeout_seconds must be between 0 and 300")

    def delay_before_retry(self, retry_number: int) -> float:
        """Return deterministic exponential backoff for a one-based retry."""

        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * float(2 ** max(0, retry_number - 1)),
        )


def _bounded_text(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise NotificationValidationError(f"{name} must be a string")
    normalized = value.strip() if name != "body" else value
    if not normalized:
        raise NotificationValidationError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise NotificationValidationError(f"{name} must not exceed {maximum} characters")
    return normalized
