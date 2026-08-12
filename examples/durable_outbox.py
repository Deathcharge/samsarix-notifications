# SPDX-License-Identifier: MPL-2.0
"""Atomically save an order and queue its notification in one SQLite commit."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from samsarix_notifications import (
    NotificationPayload,
    NotificationService,
    OutboxWorker,
    SQLiteOutbox,
    TransportResult,
)


class ReceiptTransport:
    """A visible stand-in for an application's email or webhook transport."""

    async def send(self, payload: NotificationPayload) -> TransportResult:
        print(f"sending {payload.subject!r} to {payload.recipient}")
        return TransportResult(provider_id=f"receipt-{payload.metadata['order_id']}")


async def demonstrate(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_email TEXT NOT NULL)"
        )
        connection.commit()

    outbox = SQLiteOutbox(database)
    notification = NotificationPayload(
        channel="receipt",
        recipient="customer@example.com",
        subject="Order 456 confirmed",
        body="Thanks! We received your order and will send tracking details soon.",
        metadata={"order_id": 456},
        idempotency_key="order-456-confirmation",
    )

    # Because both writes use the same SQLite connection, they commit or roll
    # back together. The network is not touched inside this transaction.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO orders (id, customer_email) VALUES (?, ?)",
            (456, notification.recipient),
        )
        queued = outbox.enqueue(notification, connection=connection)

    service = NotificationService(transports={"receipt": ReceiptTransport()})
    worker = OutboxWorker(outbox, service)
    summary = await worker.run_once()
    stored = outbox.get(queued.message.message_id)

    assert stored is not None
    print(
        f"created={queued.created} delivered={summary.delivered} "
        f"durable_status={stored.status.value} attempts={stored.attempt_count}"
    )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="samsarix-outbox-") as directory:
        asyncio.run(demonstrate(Path(directory) / "application.sqlite3"))
