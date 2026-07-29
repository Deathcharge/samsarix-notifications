# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from samsarix_notifications import (
    NotificationPayload,
    NotificationValidationError,
    RetryPolicy,
)


def base_payload(**overrides: object) -> NotificationPayload:
    values: dict[str, object] = {
        "channel": "webhook",
        "recipient": "https://example.com",
        "subject": "Subject",
        "body": "Body",
    }
    values.update(overrides)
    return NotificationPayload(**values)  # type: ignore[arg-type]


def test_payload_normalizes_channel_and_naive_timestamp() -> None:
    request = base_payload(channel=" WEBHOOK ", created_at=datetime(2026, 1, 1))
    assert request.channel == "webhook"
    assert request.created_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", 1),
        ("recipient", None),
        ("subject", ""),
        ("body", ""),
        ("notification_id", "x" * 129),
        ("idempotency_key", "x" * 129),
        ("retry_count", -1),
        ("max_retries", 11),
    ],
)
def test_payload_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(NotificationValidationError):
        base_payload(**{field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"max_retries": -1},
        {"base_delay_seconds": -1},
        {"max_delay_seconds": 301},
        {"base_delay_seconds": 2, "max_delay_seconds": 1},
        {"attempt_timeout_seconds": 0},
    ],
)
def test_retry_policy_rejects_unbounded_values(values: dict[str, object]) -> None:
    with pytest.raises(NotificationValidationError):
        RetryPolicy(**values)  # type: ignore[arg-type]
