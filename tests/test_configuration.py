# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from samsarix_notifications import (
    AlertSystem,
    ConfigurationError,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    OutboxWorker,
    RetryPolicy,
    SMTPConfig,
    SQLiteOutbox,
    WebhookPolicy,
)


class NumericLookalike:
    def __int__(self) -> int:
        raise AssertionError("configuration must not coerce custom values")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("configuration must not compare custom values")

    def __le__(self, other: object) -> bool:
        raise AssertionError("configuration must not compare custom values")

    def __gt__(self, other: object) -> bool:
        raise AssertionError("configuration must not compare custom values")

    def __ge__(self, other: object) -> bool:
        raise AssertionError("configuration must not compare custom values")


def make_config(kind: str, tmp_path: Path, values: dict[str, Any]) -> object:
    """Exercise public constructors, never private validation helpers."""
    if kind == "service":
        return NotificationService(**values)
    if kind == "service_mapping":
        return NotificationService(config=values)
    if kind == "retry":
        return RetryPolicy(**{"base_delay_seconds": 0, "max_delay_seconds": 300, **values})
    if kind == "payload":
        return NotificationPayload(
            **{
                "channel": "webhook",
                "recipient": "https://example.com/events",
                "subject": "Order confirmed",
                "body": "Synthetic event",
                "max_retries": 10,
                **values,
            }
        )
    if kind == "alerts":
        return AlertSystem(**values)
    if kind in {"smtp", "smtp_mapping"}:
        settings = {"host": "smtp.example.com", "from_address": "app@example.com", **values}
        return (
            SMTPConfig.from_mapping(settings) if kind == "smtp_mapping" else SMTPConfig(**settings)
        )
    if kind == "webhook":
        return WebhookPolicy(**values)
    if kind == "outbox":
        return SQLiteOutbox(tmp_path / "not-created.sqlite3", **{"initialize": False, **values})
    if kind == "worker":
        return OutboxWorker(
            SQLiteOutbox(tmp_path / "not-created.sqlite3", initialize=False),
            NotificationService(),
            **{"base_delay_seconds": 0, "max_delay_seconds": 86_400, **values},
        )
    raise AssertionError(f"Unknown configuration kind: {kind}")


@pytest.mark.parametrize(
    ("kind", "field", "minimum", "maximum"),
    [
        (kind, field, minimum, maximum)
        for kind in ("service", "service_mapping")
        for field, minimum, maximum in (
            ("concurrency_limit", 1, 1_000),
            ("max_batch_size", 1, 10_000),
            ("history_limit", 0, 100_000),
            ("idempotency_cache_size", 0, 100_000),
            ("max_pending_deliveries", 1, 100_000),
        )
    ]
    + [
        ("retry", "max_retries", 0, 10),
        ("payload", "retry_count", 0, 10),
        ("payload", "max_retries", 0, 10),
        ("alerts", "max_alerts", 1, 1_000_000),
        ("smtp", "port", 1, 65_535),
        ("smtp_mapping", "port", 1, 65_535),
        ("smtp", "max_message_bytes", 1_024, 50 * 1024 * 1024),
        ("webhook", "max_payload_bytes", 1_024, 10 * 1024 * 1024),
        ("webhook", "max_connections", 1, 1_000),
        ("outbox", "max_messages", 1, 1_000_000),
        ("worker", "max_delivery_attempts", 1, 100),
    ],
)
def test_integer_configuration_contract(
    kind: str, field: str, minimum: int, maximum: int, tmp_path: Path
) -> None:
    expected = (
        ConfigurationError
        if kind in {"smtp", "smtp_mapping", "webhook"}
        else NotificationValidationError
    )
    expected_code = "invalid_configuration" if expected is ConfigurationError else "invalid_input"
    invalid: list[Any] = [
        True,
        False,
        float(minimum),
        minimum + 0.5,
        str(minimum),
        None,
        float("nan"),
        float("inf"),
        float("-inf"),
        minimum - 1,
        maximum + 1,
        10**1000,
        NumericLookalike(),
    ]
    for value in invalid:
        with pytest.raises(expected) as caught:
            make_config(kind, tmp_path, {field: value})
        assert caught.value.code == expected_code
        assert not caught.value.retryable
    for value in (minimum, maximum):
        make_config(kind, tmp_path, {field: value})
    assert not (tmp_path / "not-created.sqlite3").exists()


@pytest.mark.parametrize(
    ("kind", "field", "minimum", "maximum", "exclusive_minimum"),
    [
        ("retry", "base_delay_seconds", 0, 60, False),
        ("retry", "max_delay_seconds", 0, 300, False),
        ("retry", "attempt_timeout_seconds", 0, 300, True),
        ("service", "shutdown_timeout_seconds", 0, 300, True),
        ("service_mapping", "shutdown_timeout_seconds", 0, 300, True),
        ("smtp", "timeout_seconds", 0, 300, True),
        ("smtp_mapping", "timeout_seconds", 0, 300, True),
        ("webhook", "timeout_seconds", 0, 300, True),
        ("outbox", "busy_timeout_seconds", 0, 300, True),
        ("worker", "lease_seconds", 1, 86_400, False),
        ("worker", "base_delay_seconds", 0, 3_600, False),
        ("worker", "max_delay_seconds", 0, 86_400, False),
    ],
)
def test_duration_configuration_contract(
    kind: str, field: str, minimum: int, maximum: int, exclusive_minimum: bool, tmp_path: Path
) -> None:
    expected = (
        ConfigurationError
        if kind in {"smtp", "smtp_mapping", "webhook"}
        else NotificationValidationError
    )
    invalid: list[Any] = [
        True,
        False,
        "1",
        None,
        float("nan"),
        float("inf"),
        float("-inf"),
        minimum - 0.01,
        maximum + 0.01,
        10**1000,
        NumericLookalike(),
    ]
    if exclusive_minimum:
        invalid.append(minimum)
    for value in invalid:
        with pytest.raises(expected) as caught:
            make_config(kind, tmp_path, {field: value})
        assert not caught.value.retryable
    for value in (minimum + 0.125, maximum, float(maximum)):
        make_config(kind, tmp_path, {field: value})
    if not exclusive_minimum:
        make_config(kind, tmp_path, {field: minimum})
    assert not (tmp_path / "not-created.sqlite3").exists()


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("smtp", "start_tls"),
        ("smtp", "use_ssl"),
        ("smtp_mapping", "start_tls"),
        ("smtp_mapping", "use_ssl"),
        ("webhook", "allow_private_addresses"),
        ("outbox", "initialize"),
    ],
)
def test_boolean_options_do_not_accept_truthy_strings_or_numbers(
    kind: str, field: str, tmp_path: Path
) -> None:
    expected = NotificationValidationError if kind == "outbox" else ConfigurationError
    invalid: list[Any] = ["false", "true", 0, 1, None, [], NumericLookalike()]
    for value in invalid:
        with pytest.raises(expected):
            make_config(kind, tmp_path, {field: value})
        assert not (tmp_path / "not-created.sqlite3").exists()
    for value in (False, True):
        settings: dict[str, Any] = {field: value}
        if field == "use_ssl":
            settings["start_tls"] = False
        make_config(kind, tmp_path, settings)


def test_webhook_ports_require_native_integers() -> None:
    invalid: list[Any] = [True, 443.0, 443.5, "443", None, float("nan"), 0, 65_536]
    for value in invalid:
        with pytest.raises(ConfigurationError):
            WebhookPolicy(allowed_ports=frozenset({value}))
    WebhookPolicy(allowed_ports=frozenset({1, 443, 65_535}))


@pytest.mark.parametrize("field", ["max_messages", "busy_timeout_seconds", "initialize"])
def test_invalid_outbox_constructor_does_not_create_database(field: str, tmp_path: Path) -> None:
    database = tmp_path / "absent.sqlite3"
    with pytest.raises(NotificationValidationError):
        SQLiteOutbox(database, **{field: "false"})  # type: ignore[arg-type]
    assert not database.exists()


async def test_outbox_operation_limits_fail_before_database_access(tmp_path: Path) -> None:
    database = tmp_path / "absent.sqlite3"
    store = SQLiteOutbox(database, initialize=False)
    async with NotificationService() as service:
        worker = OutboxWorker(store, service)
        stopped = asyncio.Event()
        stopped.set()
        invalid: list[Any] = [True, False, 1.0, 1.5, "1", None, 0, 10_001, float("nan")]
        for value in invalid:
            with pytest.raises(NotificationValidationError):
                store.list_messages(limit=value)
            with pytest.raises(NotificationValidationError):
                store.purge_completed(before=datetime.now(timezone.utc), limit=value)
            with pytest.raises(NotificationValidationError):
                await worker.run_once(limit=value)
            with pytest.raises(NotificationValidationError):
                await worker.run_forever(batch_size=value, stop_event=stopped)
        invalid_intervals: list[Any] = [
            True,
            "1",
            None,
            0,
            0.009,
            300.01,
            float("nan"),
            float("inf"),
        ]
        for value in invalid_intervals:
            with pytest.raises(NotificationValidationError):
                await worker.run_forever(poll_interval_seconds=value, stop_event=stopped)
        for batch_size in (1, 10_000):
            for poll_interval in (0.01, 0.125, 300):
                await worker.run_forever(
                    batch_size=batch_size, poll_interval_seconds=poll_interval, stop_event=stopped
                )
    assert not database.exists()


def test_backoff_requires_bounded_one_based_integer_before_exponentiation() -> None:
    policy = RetryPolicy(base_delay_seconds=0.125, max_delay_seconds=10)
    invalid: list[Any] = [True, False, 0, -1, 11, 1.0, 1.5, "1", None, 10**1000]
    for value in invalid:
        with pytest.raises(NotificationValidationError):
            policy.delay_before_retry(value)
    assert policy.delay_before_retry(1) == 0.125
    assert policy.delay_before_retry(2) == 0.25
    assert policy.delay_before_retry(10) == 10
    assert RetryPolicy(base_delay_seconds=0, max_delay_seconds=0).delay_before_retry(10) == 0


def test_fractional_retry_budget_is_rejected_at_construction() -> None:
    with pytest.raises(NotificationValidationError):
        RetryPolicy(max_retries=0.5)  # type: ignore[arg-type]


def test_fractional_dispatcher_capacity_is_not_silently_truncated() -> None:
    with pytest.raises(NotificationValidationError):
        NotificationService(config={"concurrency_limit": 1.5})


def test_fractional_outbox_limit_fails_before_sqlite_execution(tmp_path: Path) -> None:
    store = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(NotificationValidationError):
        store.list_messages(limit=1.5)  # type: ignore[arg-type]
