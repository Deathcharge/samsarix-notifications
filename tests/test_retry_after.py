# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from samsarix_notifications import (
    DeliveryError,
    DeliveryResult,
    DeliveryStatus,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    OutboxStatus,
    OutboxWorker,
    RetryPolicy,
    SQLiteOutbox,
    WebhookPolicy,
    WebhookRouter,
)
from samsarix_notifications.models import utc_now


async def public_resolver(_host: str, _port: int) -> Sequence[str]:
    return ["93.184.216.34"]


def payload() -> NotificationPayload:
    return NotificationPayload(
        channel="webhook",
        recipient="https://example.com/events",
        subject="Order confirmed",
        body="Synthetic event",
        idempotency_key="order-123",
    )


def router_for(client: httpx.AsyncClient) -> WebhookRouter:
    return WebhookRouter(
        client=client,
        resolver=public_resolver,
        policy=WebhookPolicy(allowed_hosts=frozenset({"example.com"})),
    )


async def test_direct_retry_does_not_shorten_provider_delay_to_local_cap() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(
            transports={"webhook": router_for(client)},
            retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
        ) as service:
            result = await service.send(payload())
    assert calls == 1
    assert result.attempts == 1 and not result.success and result.retryable
    assert result.retry_after_seconds == 60
    assert result.as_dict()["retry_after_seconds"] == 60


async def test_outbox_does_not_reclaim_throttled_message_immediately(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    store = SQLiteOutbox(tmp_path / "orders.sqlite3")
    store.enqueue(payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            worker = OutboxWorker(store, service, base_delay_seconds=0, max_delay_seconds=0)
            result = await worker.run_once(limit=3)
    assert calls == 1
    assert result.claimed == 1 and result.rescheduled == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("0", 0),
        ("000002", 2),
        (" \t15\t ", 15),
        ("86400", 86_400),
        ("-1", None),
        ("0.5", None),
        ("1e2", None),
        ("NaN", None),
        ("inf", None),
        ("invalid-date", None),
        ("1, 2", None),
        ("Mon, 31 Aug 2026 12:01:00 GMT", 60),
        ("Monday, 31-Aug-26 12:01:00 GMT", 60),
        ("Mon Aug 31 12:01:00 2026", 60),
        ("Mon, 31 Aug 2026 11:59:00 GMT", 0),
        ("Mon, 31 Aug 2026 13:01:00 +0100", 60),
    ],
)
async def test_webhook_retry_after_parsing(header: str | None, expected: float | None) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={} if header is None else {"Retry-After": header})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with patch("samsarix_notifications.webhook_router.utc_now", return_value=now):
            with pytest.raises(DeliveryError) as caught:
                await router_for(client).send(payload())
    assert caught.value.code == "webhook_http_error"
    assert caught.value.retryable
    assert caught.value.retry_after_seconds == expected


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
async def test_retryable_http_status_preserves_minimum_delay(status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, headers={"Retry-After": "2"})
        )
    ) as client:
        with pytest.raises(DeliveryError) as caught:
            await router_for(client).send(payload())
    assert caught.value.retry_after_seconds == 2


@pytest.mark.parametrize("status", [202, 301, 400, 401, 403, 404])
async def test_header_does_not_enable_retries_for_other_statuses(status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, headers={"Retry-After": "9" * 129})
        )
    ) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            result = await service.send(payload())
    assert result.attempts == 1
    assert result.success is (status == 202)
    assert result.retry_after_seconds is None
    assert result.retryable is (None if status == 202 else False)


@pytest.mark.parametrize(
    "header", ["86401", "999999", "9" * 128, "9" * 129, "0" * 129, "Wed, 02 Sep 2026 12:00:00 GMT"]
)
async def test_unsupported_waits_stop_automatic_delivery(header: str, tmp_path: Path) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": header})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            with patch("samsarix_notifications.webhook_router.utc_now", return_value=now):
                result = await service.send(payload())
                assert result.attempts == 1 and result.retryable is False
                assert result.error_code == "webhook_retry_after_unsupported"
                assert result.retry_after_seconds is None
                assert header not in (result.error_message or "")
                store = SQLiteOutbox(tmp_path / "outbox.sqlite3")
                queued = store.enqueue(payload())
                outcome = await OutboxWorker(store, service).run_once(limit=3)
                stored = store.get(queued.message.message_id)
    assert calls == 2
    assert outcome.dead_lettered == 1
    assert stored is not None and stored.status is OutboxStatus.DEAD_LETTER


@pytest.mark.parametrize(
    ("header", "backoff", "expected"), [("2", 0.125, 2), ("0", 3, 3), ("invalid", 0.25, 0.25)]
)
async def test_direct_waits_for_greater_of_local_and_provider_backoff(
    header: str, backoff: float, expected: float
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(503, headers={"Retry-After": header})
            if calls == 1
            else httpx.Response(202)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(
            transports={"webhook": router_for(client)},
            retry_policy=RetryPolicy(base_delay_seconds=backoff),
            sleep=sleep,
        ) as service:
            request = payload()
            result = await service.send(request)
            duplicate = await service.send(request)
    assert sleeps == [expected]
    assert calls == 2 and result.success and result.attempts == 2
    assert result.retry_after_seconds is None
    assert duplicate.deduplicated and duplicate.retry_after_seconds is None


async def test_only_latest_failure_supplies_retry_hint() -> None:
    calls = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "1"} if calls == 1 else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(
            transports={"webhook": router_for(client)},
            sleep=sleep,
            retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0),
        ) as service:
            result = await service.send(payload())
    assert sleeps == [1, 0]
    assert result.attempts == 3 and result.retry_after_seconds is None


async def test_exhausted_retry_budget_still_exposes_final_hint() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": "1"})
        )
    ) as client:
        async with NotificationService(
            transports={"webhook": router_for(client)},
            retry_policy=RetryPolicy(max_retries=0),
        ) as service:
            result = await service.send(payload())
    assert result.attempts == 1 and result.retry_after_seconds == 1 and result.retryable


async def test_durable_hint_survives_reopen_and_does_not_block_other_messages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox.sqlite3"
    store = SQLiteOutbox(database)
    request = payload()
    store.enqueue(request)
    store.enqueue(replace(request, notification_id="second", idempotency_key="second"))
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "60"})
            if calls == 1
            else httpx.Response(202)
        )

    before = utc_now()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            first = await OutboxWorker(
                store, service, base_delay_seconds=0, max_delay_seconds=0
            ).run_once(limit=3)
    assert first.claimed == 2 and first.delivered == 1 and first.rescheduled == 1
    reopened = SQLiteOutbox(database)
    pending = reopened.get(request.notification_id)
    assert pending is not None and pending.status is OutboxStatus.PENDING
    assert pending.available_at >= before + timedelta(seconds=60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            restarted = OutboxWorker(reopened, service)
            assert (await restarted.run_once()).claimed == 0
            with patch("samsarix_notifications.outbox.utc_now", return_value=pending.available_at):
                assert (await restarted.run_once()).delivered == 1
    assert calls == 3


async def test_http_date_is_preserved_as_durable_minimum(tmp_path: Path) -> None:
    deadline = (utc_now() + timedelta(minutes=5)).replace(microsecond=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                503, headers={"Retry-After": format_datetime(deadline, usegmt=True)}
            )
        )
    ) as client:
        async with NotificationService(transports={"webhook": router_for(client)}) as service:
            store = SQLiteOutbox(tmp_path / "outbox.sqlite3")
            request = payload()
            store.enqueue(request)
            outcome = await OutboxWorker(store, service).run_once(limit=2)
            saved = store.get(request.notification_id)
    assert outcome.rescheduled == 1
    assert saved is not None and saved.available_at >= deadline


def test_public_retry_hint_validates_numbers_and_failure_state() -> None:
    now = utc_now()
    result = DeliveryResult(
        "n", "test", "recipient", DeliveryStatus.FAILED, 1, now, now, retryable=True
    )
    invalid: list[Any] = [True, False, -1, 86_400.1, float("nan"), float("inf"), "1", 10**1000]
    for value in invalid:
        with pytest.raises(NotificationValidationError):
            DeliveryError("busy", retryable=True, retry_after_seconds=value)
        with pytest.raises(NotificationValidationError):
            replace(result, retry_after_seconds=value)
    for value in (0, 0.125, 86_400):
        assert (
            DeliveryError("busy", retryable=True, retry_after_seconds=value).retry_after_seconds
            == value
        )
        assert replace(result, retry_after_seconds=value).as_dict()["retry_after_seconds"] == value
    with pytest.raises(NotificationValidationError):
        DeliveryError("permanent", retry_after_seconds=1)
    with pytest.raises(NotificationValidationError):
        replace(result, retryable=False, retry_after_seconds=1)
    with pytest.raises(NotificationValidationError):
        replace(result, status=DeliveryStatus.DELIVERED, retry_after_seconds=1)
