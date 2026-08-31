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

The example rolls back a transaction, commits an order and notification together, then starts two independent worker processes against a real loopback HTTP receiver. The first receives HTTP 503; the restarted worker receives HTTP 202. It verifies the stored receipt and duplicate enqueue against `examples/fixtures/order_confirmed_v1.json`. Expected summary: `worker_processes=2`, `http_attempts=2`, `accepted_events=1`, `durable_status="delivered"`, `duplicate_created=false`. The consumer and data are synthetic; this does not prove production adoption or exactly-once processing. No credentials or external network calls are needed, and its temporary database is removed after the demonstration.

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

Choose a lease longer than the maximum expected single provider attempt. `OutboxWorker` disables the dispatcher's inner retry loop and makes one provider call per durable claim, so `max_delivery_attempts` is the total provider-call budget. The default five-minute lease safely exceeds the default per-attempt timeout. Provider-side idempotency is still recommended whenever the provider supports it.

Every claim has a fresh random lease token in addition to its worker ID. A stale attempt therefore cannot finalize a newer claim even when two processes reuse the same configured worker ID or an operator dead-letters and requeues the message. Records that fail stored-payload, timestamp, status, or counter integrity checks are moved to `dead_letter` with a generic corruption code before any transport is called; quarantines count against the requested run limit and the worker continues with healthy rows while capacity remains. Corrupt payload content remains unreadable through the normal message API and should be inspected or removed only through an application-controlled database recovery procedure.

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

### Backpressure and retention

`SQLiteOutbox(path, max_messages=10_000)` caps **all retained rows**, including delivered and dead-letter records. The integer limit must be from 1 to 1,000,000. New inserts at capacity raise `OutboxConflictError` with `code="outbox_capacity_reached"` and `retryable=True`; matching duplicate requests remain readable and return `created=False`. The capacity predicate and insert are one atomic SQL statement, including when the caller owns an autocommit connection. Let this exception roll back your application transaction, then retry only after an operator or retention job has freed capacity. Do not acknowledge an order whose transaction rolled back.

All writers for a database should use the same configured cap. The limit is per-instance policy, not a constraint on raw SQL or another application configured with a higher cap. Existing rows are never deleted merely because a lower cap is configured. Delivery alone does not free a slot: call `purge_completed` for delivered rows outside your business replay window. Purging also removes durable deduplication history, so subsequent replay can create a new delivery. Dead letters require deliberate recovery/retention, not automatic deletion.

This is a row limit, not a physical-disk quota. Choose a substantially lower cap for constrained hosts, monitor disk usage, and account for SQLite journals, free pages, other application tables, and backups. At the 2 MiB record maximum, 10,000 rows could contain about 20 GiB of payload data before overhead. A row deletion makes space reusable inside SQLite; it does not guarantee file shrinking or secure erasure.

## Privacy and storage

The SQLite database contains notification recipients, subjects, bodies, JSON metadata, and provider receipts in plaintext. Store it in a private directory, restrict filesystem permissions, include it in backup and disk-encryption policy where appropriate, and never put credentials or secrets in notification metadata. The outbox does not log message content or transmit telemetry.

Metadata must contain JSON-native values with string object keys (tuples are accepted as arrays); non-finite floats and unsupported objects are rejected. The complete record is capped at 2 MiB. Before encoding, the record is limited to 32 levels of nesting, 10,000 visited values/keys, a cumulative text budget, and integers of at most 4,096 bits. Excessive nesting, cycles, or expansion produce `outbox_payload_too_complex`; byte overflow produces `outbox_payload_too_large`; unsupported values produce `outbox_payload_not_json`. The envelope counts toward these limits. In-memory email attachments are intentionally not serialized; persist large artifacts separately and enqueue a stable reference or use a custom transport boundary.

## Scope boundary

SQLite serializes writers and is a strong fit for a single machine with modest notification volume. For high-volume, multi-region, or independently scaled workers, use an external durable broker and keep `NotificationService` as the typed delivery boundary. Subscriber preferences, consent, localization, digests, and cross-workflow orchestration remain separate product concerns.
