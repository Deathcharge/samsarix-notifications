# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from samsarix_notifications import (
    DeliveryError,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    OutboxConflictError,
    OutboxLeaseError,
    OutboxStatus,
    OutboxWorker,
    RetryPolicy,
    SQLiteOutbox,
    TransportResult,
)
from samsarix_notifications.models import utc_now


class ScriptedTransport:
    def __init__(self, outcomes: Sequence[TransportResult | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.payloads: list[NotificationPayload] = []

    async def send(self, request: NotificationPayload) -> TransportResult:
        self.payloads.append(request)
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def payload(**overrides: object) -> NotificationPayload:
    values: dict[str, object] = {
        "channel": "test",
        "recipient": "customer-123",
        "subject": "Order shipped",
        "body": "Order 456 is on its way.",
        "metadata": {"order_id": 456, "tags": ["transactional", "shipping"]},
        "max_retries": 0,
        "notification_id": "notification-1",
    }
    values.update(overrides)
    return NotificationPayload(**values)  # type: ignore[arg-type]


def outbox(tmp_path: Path) -> SQLiteOutbox:
    return SQLiteOutbox(tmp_path / "notifications.sqlite3")


def test_enqueue_round_trip_schedule_listing_and_counts(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    scheduled_for = utc_now() + timedelta(hours=1)

    result = store.enqueue(payload(), available_at=scheduled_for)

    assert result.created
    assert result.message.payload.metadata["order_id"] == 456
    assert result.message.available_at == scheduled_for
    assert result.message.status is OutboxStatus.PENDING
    assert store.get("notification-1") == result.message
    assert store.get("missing") is None
    assert store.list_messages(status=OutboxStatus.PENDING) == (result.message,)
    assert store.list_messages(status=OutboxStatus.DELIVERED) == ()
    assert store.counts() == {
        OutboxStatus.PENDING: 1,
        OutboxStatus.PROCESSING: 0,
        OutboxStatus.DELIVERED: 0,
        OutboxStatus.DEAD_LETTER: 0,
    }


@pytest.mark.asyncio
async def test_async_enqueue_deduplicates_semantic_request(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    first = await store.aenqueue(payload(idempotency_key="order-456-shipped"))
    duplicate = await store.aenqueue(
        payload(
            idempotency_key="order-456-shipped",
            notification_id="a-new-generated-id",
        )
    )

    assert first.created
    assert not duplicate.created
    assert duplicate.message.message_id == first.message.message_id
    assert store.counts()[OutboxStatus.PENDING] == 1


def test_idempotency_and_notification_id_conflicts_are_rejected(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload(idempotency_key="same-operation"))

    with pytest.raises(OutboxConflictError) as key_conflict:
        store.enqueue(
            payload(
                idempotency_key="same-operation",
                notification_id="another-id",
                body="A different semantic operation",
            )
        )
    assert key_conflict.value.code == "outbox_idempotency_conflict"

    with pytest.raises(OutboxConflictError):
        store.enqueue(payload(idempotency_key="another-key", body="Different"))


def test_enqueue_can_share_an_application_transaction(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    store = SQLiteOutbox(database)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    connection.commit()
    original_row_factory = connection.row_factory

    connection.execute("BEGIN")
    connection.execute("INSERT INTO orders (id, status) VALUES (456, 'paid')")
    store.enqueue(payload(), connection=connection)
    assert connection.row_factory is original_row_factory
    connection.rollback()

    assert connection.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)
    assert store.get("notification-1") is None
    connection.close()


def test_initialize_preserves_caller_transaction_ownership(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    store = SQLiteOutbox(database, initialize=False)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.execute("INSERT INTO orders (id) VALUES (456)")

    store.initialize(connection=connection)

    assert connection.in_transaction
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)
    connection.rollback()
    connection.close()


@pytest.mark.asyncio
async def test_worker_delivers_and_persists_provider_receipt(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    transport = ScriptedTransport([TransportResult("provider-42", "accepted")])
    service = NotificationService(transports={"test": transport})
    worker = OutboxWorker(store, service, worker_id="worker-one")

    summary = await worker.run_once()

    assert summary.claimed == summary.delivered == 1
    assert summary.rescheduled == summary.dead_lettered == 0
    message = store.get("notification-1")
    assert message is not None
    assert message.status is OutboxStatus.DELIVERED
    assert message.attempt_count == 1
    assert message.completed_at is not None
    assert message.provider_id == "provider-42"
    assert message.provider_status == "accepted"


@pytest.mark.asyncio
async def test_worker_reschedules_retryable_failure_then_delivers(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    transport = ScriptedTransport(
        [
            DeliveryError("provider busy", code="busy", retryable=True),
            TransportResult(provider_status="accepted"),
        ]
    )
    service = NotificationService(
        transports={"test": transport},
        retry_policy=RetryPolicy(max_retries=0),
    )
    worker = OutboxWorker(
        store,
        service,
        worker_id="retry-worker",
        max_delivery_attempts=3,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )

    first = await worker.run_once(limit=1)
    pending = store.get("notification-1")
    second = await worker.run_once(limit=1)

    assert first.rescheduled == 1
    assert pending is not None
    assert pending.status is OutboxStatus.PENDING
    assert pending.last_error_code == "busy"
    assert second.delivered == 1
    delivered = store.get("notification-1")
    assert delivered is not None
    assert delivered.status is OutboxStatus.DELIVERED
    assert delivered.attempt_count == 2


@pytest.mark.asyncio
async def test_worker_dead_letters_nonretryable_failure_and_operator_requeues(
    tmp_path: Path,
) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    transport = ScriptedTransport(
        [
            DeliveryError("invalid recipient", code="address_rejected"),
            TransportResult(provider_status="accepted"),
        ]
    )
    service = NotificationService(transports={"test": transport})
    worker = OutboxWorker(store, service, worker_id="operator-worker")

    first = await worker.run_once()
    dead = store.get("notification-1")
    requeued = store.requeue_dead_letter("notification-1")
    second = await worker.run_once()

    assert first.dead_lettered == 1
    assert dead is not None
    assert dead.status is OutboxStatus.DEAD_LETTER
    assert dead.last_error_code == "address_rejected"
    assert requeued.status is OutboxStatus.PENDING
    assert requeued.attempt_count == 0
    assert requeued.last_error_code is None
    assert second.delivered == 1
    with pytest.raises(OutboxConflictError) as conflict:
        store.requeue_dead_letter("notification-1")
    assert conflict.value.code == "outbox_message_not_dead_lettered"


@pytest.mark.asyncio
async def test_worker_dead_letters_after_attempt_budget(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    transport = ScriptedTransport([DeliveryError("offline", code="offline", retryable=True)])
    service = NotificationService(
        transports={"test": transport}, retry_policy=RetryPolicy(max_retries=0)
    )
    worker = OutboxWorker(
        store,
        service,
        worker_id="bounded-worker",
        max_delivery_attempts=2,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )

    summary = await worker.run_once(limit=10)

    assert summary.claimed == 2
    assert summary.rescheduled == 1
    assert summary.dead_lettered == 1
    message = store.get("notification-1")
    assert message is not None
    assert message.status is OutboxStatus.DEAD_LETTER
    assert message.attempt_count == 2


@pytest.mark.asyncio
async def test_future_message_is_not_claimed_until_available(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload(), available_at=utc_now() + timedelta(days=1))
    transport = ScriptedTransport([TransportResult()])
    worker = OutboxWorker(store, NotificationService(transports={"test": transport}))

    summary = await worker.run_once()

    assert summary.claimed == 0
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_and_old_owner_cannot_finalize(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    now = utc_now()
    first = store._claim(worker_id="worker-one", lease_seconds=1, now=now).message
    second = store._claim(
        worker_id="worker-two", lease_seconds=60, now=now + timedelta(seconds=2)
    ).message
    assert first is not None
    assert second is not None
    assert second.attempt_count == 2

    service = NotificationService(
        transports={"test": ScriptedTransport([TransportResult(provider_status="accepted")])}
    )
    result = await service.send(first.payload)
    with pytest.raises(OutboxLeaseError) as lost:
        store._mark_delivered(first, worker_id="worker-one", result=result)
    assert lost.value.code == "outbox_lease_lost"


@pytest.mark.asyncio
async def test_attempt_generation_fences_reused_worker_id(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    now = utc_now()
    first = store._claim(worker_id="stable-worker", lease_seconds=1, now=now).message
    second = store._claim(
        worker_id="stable-worker", lease_seconds=60, now=now + timedelta(seconds=2)
    ).message
    assert first is not None and second is not None

    service = NotificationService(
        transports={"test": ScriptedTransport([TransportResult(provider_status="accepted")])}
    )
    result = await service.send(first.payload)
    with pytest.raises(OutboxLeaseError, match="no longer owned"):
        store._mark_delivered(first, worker_id="stable-worker", result=result)

    store._mark_delivered(second, worker_id="stable-worker", result=result)
    delivered = store.get("notification-1")
    assert delivered is not None
    assert delivered.status is OutboxStatus.DELIVERED


@pytest.mark.asyncio
async def test_run_forever_stops_cleanly(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    service = NotificationService(transports={"test": ScriptedTransport([TransportResult()])})
    worker = OutboxWorker(store, service)
    stop = asyncio.Event()

    task = asyncio.create_task(
        worker.run_forever(poll_interval_seconds=0.01, batch_size=1, stop_event=stop)
    )
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_run_forever_drains_before_stopping(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    transport = ScriptedTransport([TransportResult()])
    worker = OutboxWorker(store, NotificationService(transports={"test": transport}))
    stop = asyncio.Event()

    task = asyncio.create_task(
        worker.run_forever(poll_interval_seconds=0.01, batch_size=1, stop_event=stop)
    )
    for _ in range(100):
        message = store.get("notification-1")
        if message is not None and message.status is OutboxStatus.DELIVERED:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert transport.calls == 1


@pytest.mark.asyncio
async def test_purge_completed_is_bounded(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    for number in range(2):
        store.enqueue(payload(notification_id=f"notification-{number}"))
    worker = OutboxWorker(
        store,
        NotificationService(transports={"test": ScriptedTransport([TransportResult()])}),
    )
    assert (await worker.run_once()).delivered == 2

    assert store.purge_completed(before=utc_now() + timedelta(seconds=1), limit=1) == 1
    assert store.counts()[OutboxStatus.DELIVERED] == 1


def test_validation_and_corrupt_storage_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(NotificationValidationError):
        SQLiteOutbox(":memory:")
    with pytest.raises(NotificationValidationError):
        SQLiteOutbox(tmp_path / "bad.sqlite3", busy_timeout_seconds=0)

    store = outbox(tmp_path)
    with pytest.raises(NotificationValidationError, match="timezone-aware"):
        store.enqueue(payload(), available_at=utc_now().replace(tzinfo=None))
    with pytest.raises(NotificationValidationError) as not_json:
        store.enqueue(payload(metadata={"value": object()}))
    assert not_json.value.code == "outbox_payload_not_json"
    with pytest.raises(NotificationValidationError):
        store.list_messages(limit=0)
    with pytest.raises(NotificationValidationError):
        store.list_messages(status="pending")  # type: ignore[arg-type]
    with pytest.raises(NotificationValidationError):
        store.get("")
    with pytest.raises(NotificationValidationError):
        store.purge_completed(before=utc_now(), limit=0)

    store.enqueue(payload())
    with closing(sqlite3.connect(store.database)) as connection:
        connection.execute("UPDATE samsarix_notification_outbox SET payload_json = 'not-json'")
        connection.commit()
    with pytest.raises(OutboxConflictError) as corrupt:
        store.get("notification-1")
    assert corrupt.value.code == "outbox_payload_corrupt"


@pytest.mark.asyncio
async def test_worker_quarantines_payload_corruption_and_continues(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    store.enqueue(payload(notification_id="notification-2", recipient="customer-2"))
    with closing(sqlite3.connect(store.database)) as connection:
        encoded = connection.execute(
            "SELECT payload_json FROM samsarix_notification_outbox WHERE message_id = ?",
            ("notification-1",),
        ).fetchone()[0]
        changed = json.loads(encoded)
        changed["recipient"] = "attacker-controlled"
        connection.execute(
            "UPDATE samsarix_notification_outbox SET payload_json = ? WHERE message_id = ?",
            (json.dumps(changed), "notification-1"),
        )
        connection.commit()
    transport = ScriptedTransport([TransportResult(provider_status="accepted")])
    worker = OutboxWorker(store, NotificationService(transports={"test": transport}))

    summary = await worker.run_once()

    assert summary.dead_lettered == 1
    assert summary.delivered == summary.claimed == 1
    assert [request.recipient for request in transport.payloads] == ["customer-2"]
    assert store.counts()[OutboxStatus.DEAD_LETTER] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("timestamp", "status", "counter"))
async def test_worker_quarantines_structurally_corrupt_rows(
    tmp_path: Path,
    corruption: str,
) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload())
    store.enqueue(payload(notification_id="notification-2"))
    with closing(sqlite3.connect(store.database)) as connection:
        if corruption == "timestamp":
            connection.execute(
                "UPDATE samsarix_notification_outbox SET available_at = 'not-a-time' "
                "WHERE message_id = 'notification-1'"
            )
        else:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            if corruption == "status":
                connection.execute(
                    "UPDATE samsarix_notification_outbox SET status = 'broken' "
                    "WHERE message_id = 'notification-1'"
                )
            else:
                connection.execute(
                    "UPDATE samsarix_notification_outbox SET attempt_count = 'broken' "
                    "WHERE message_id = 'notification-1'"
                )
        connection.commit()
    transport = ScriptedTransport([TransportResult()])
    worker = OutboxWorker(store, NotificationService(transports={"test": transport}))

    summary = await worker.run_once()

    assert summary.dead_lettered == 1
    assert summary.delivered == 1
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_worker_attempt_budget_is_total_provider_call_budget(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    store.enqueue(payload(max_retries=2))
    transport = ScriptedTransport([DeliveryError("offline", code="offline", retryable=True)])
    service = NotificationService(
        transports={"test": transport},
        retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0, max_delay_seconds=0),
    )
    worker = OutboxWorker(
        store,
        service,
        max_delivery_attempts=3,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )

    summary = await worker.run_once(limit=10)

    assert summary.claimed == transport.calls == 3
    assert summary.dead_lettered == 1


def test_worker_configuration_is_bounded(tmp_path: Path) -> None:
    store = outbox(tmp_path)
    service = NotificationService()
    with pytest.raises(NotificationValidationError):
        OutboxWorker(store, service, lease_seconds=0)
    with pytest.raises(NotificationValidationError):
        OutboxWorker(store, service, max_delivery_attempts=0)
    with pytest.raises(NotificationValidationError):
        OutboxWorker(store, service, base_delay_seconds=-1)
    with pytest.raises(NotificationValidationError):
        OutboxWorker(store, service, base_delay_seconds=10, max_delay_seconds=5)
    with pytest.raises(NotificationValidationError):
        OutboxWorker(store, service, worker_id="")
