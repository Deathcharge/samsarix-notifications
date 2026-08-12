# Durable SQLite outbox

`SQLiteOutbox` closes the reliability gap between committing application data and making a network request. It persists a validated `NotificationPayload` before delivery, and `OutboxWorker` later claims and sends it through `NotificationService`.

This is useful for single-host services, desktop software, automation, edge devices, and small deployments that need crash recovery without operating Redis, RabbitMQ, or a hosted notification platform. It is not intended to replace a high-throughput distributed broker.

## Transactional order-receipt example

When application data and the outbox share a SQLite database, pass the application's connection to `enqueue`. Samsarix never commits or rolls back a caller-owned connection.

```python
import sqlite3

from samsarix_notifications import NotificationPayload, SQLiteOutbox

outbox = SQLiteOutbox("application.sqlite3")
connection = sqlite3.connect("application.sqlite3")

try:
    with connection:
        connection.execute(
            "INSERT INTO orders (id, status) VALUES (?, ?)",
            (456, "paid"),
        )
        outbox.enqueue(
            NotificationPayload(
                channel="email",
                recipient="customer@example.com",
                subject="Payment received",
                body="We received payment for order 456.",
                metadata={"order_id": 456},
                idempotency_key="order-456-payment-receipt",
            ),
            connection=connection,
        )
finally:
    connection.close()
```

If either insert fails, neither row commits. Network delivery happens after the transaction, so a slow provider does not hold the application's transaction open.

Run the complete credential-free example with:

```bash
python examples/durable_outbox.py
```

## Worker lifecycle

```python
import asyncio

from samsarix_notifications import NotificationService, OutboxWorker, SQLiteOutbox


async def serve(notifications: NotificationService) -> None:
    outbox = SQLiteOutbox("application.sqlite3")
    worker = OutboxWorker(
        outbox,
        notifications,
        lease_seconds=300,
        max_delivery_attempts=5,
    )
    await worker.run_forever(poll_interval_seconds=1, batch_size=100)


# asyncio.run(serve(configured_notification_service))
```

`run_once` is convenient for cron jobs, tests, serverless hooks, and graceful application shutdown. `run_forever` is intended for an application-owned background task and propagates cancellation normally.

## Delivery guarantees

- Enqueue is durable once its SQLite transaction commits.
- Claims use short `BEGIN IMMEDIATE` transactions and leases, allowing multiple processes to cooperate without delivering the same available row concurrently.
- An expired lease is eligible for recovery by another worker after a crash.
- Delivery is **at least once**, not exactly once. A worker can crash after the provider accepts a notification but before SQLite records success.
- Stable idempotency keys let the outbox deduplicate across processes and restarts. Reusing a key or notification ID with different semantic content raises `OutboxConflictError` instead of silently discarding work.
- Retryable failures return to the pending queue with bounded exponential delay. Permanent failures and exhausted delivery budgets enter `dead_letter`.
- `requeue_dead_letter` is an explicit operator action and resets the worker-attempt budget.

Choose a lease longer than the maximum expected `NotificationService.send` duration, including its transport retries. The default five-minute lease safely exceeds the default dispatcher budget. Provider-side idempotency is still recommended whenever the provider supports it.

## Scheduling and operations

Supply a timezone-aware `available_at` to `enqueue` or `aenqueue` for delayed delivery. Use `counts` for bounded queue health, `list_messages` for inspection, and `purge_completed` to apply a retention policy to delivered records.

```python
from datetime import timedelta

from samsarix_notifications.models import utc_now

outbox.enqueue(payload, available_at=utc_now() + timedelta(hours=24))
print(outbox.counts())
outbox.purge_completed(before=utc_now() - timedelta(days=30), limit=1_000)
```

The outbox deliberately retains dead letters until an operator requeues them or handles the database row under an application-defined retention policy.

## Privacy and storage

The SQLite database contains notification recipients, subjects, bodies, JSON metadata, and provider receipts in plaintext. Store it in a private directory, restrict filesystem permissions, include it in backup and disk-encryption policy where appropriate, and never put credentials or secrets in notification metadata. The outbox does not log message content or transmit telemetry.

Metadata must be JSON-serializable, and the complete encoded record is capped at 2 MiB. In-memory email attachments are intentionally not serialized into the outbox; persist large artifacts separately and enqueue a stable reference or use a custom codec/transport boundary.

## Scope boundary

SQLite serializes writers and is a strong fit for a single machine with modest notification volume. For high-volume, multi-region, or independently scaled workers, use an external durable broker and keep `NotificationService` as the typed delivery boundary. Subscriber preferences, consent, localization, digests, and cross-workflow orchestration remain separate product concerns.
