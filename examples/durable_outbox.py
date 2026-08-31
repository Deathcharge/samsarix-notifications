# SPDX-License-Identifier: MPL-2.0
"""Transactional order -> durable worker -> real local HTTP consumer contract.

Run with an installed package (editable or wheel). No credentials, paid calls,
external destinations, or permanent files are used. --worker starts the child
process used to demonstrate a complete process restart between attempts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from samsarix_notifications import (
    NotificationPayload,
    NotificationService,
    OutboxStatus,
    OutboxWorker,
    SQLiteOutbox,
    WebhookPolicy,
    WebhookRouter,
)

SCRIPT = Path(__file__).resolve()
CONTRACT = SCRIPT.parent / "fixtures" / "order_confirmed_v1.json"


async def drain(database: Path, port: int) -> dict[str, int]:
    # Deliberate loopback-only policy for this example, not production defaults.
    router = WebhookRouter(
        policy=WebhookPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port}),
            allow_private_addresses=True,
        )
    )
    async with NotificationService(transports={"webhook": router}) as service:
        worker = OutboxWorker(
            SQLiteOutbox(database),
            service,
            max_delivery_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
        return asdict(await worker.run_once(limit=1))


def start_worker(database: Path, port: int) -> dict[str, int]:
    # -I ignores PYTHONPATH and cwd imports. Each invocation starts with fresh
    # process-local history/idempotency; only committed SQLite state survives.
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--worker", str(database), str(port)],
        cwd=database.parent,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    summary: dict[str, int] = json.loads(result.stdout)
    return summary


def demonstrate(database: Path) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    accepted: list[dict[str, object]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self.connection.settimeout(5)
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/orders" or not 1 <= length <= 4096 or len(attempts) >= 2:
                self.send_error(400)
                return
            event = json.loads(self.rfile.read(length))
            attempts.append(event)
            # Model a temporary provider outage, then acceptance after restart.
            status = 503 if len(attempts) == 1 else 202
            if status == 202:
                accepted.append(event)
            self.send_response(status)
            self.send_header("X-Request-ID", "order-consumer-v1")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        outbox = SQLiteOutbox(database, max_messages=100)
        notification = NotificationPayload(
            channel="webhook",
            recipient=f"http://127.0.0.1:{port}/orders",
            subject="Order 456 confirmed",
            body="Your order is ready for fulfillment.",
            metadata={"event_type": "order.confirmed", "schema_version": 1, "order_id": 456},
            idempotency_key="order-456-confirmation",
            notification_id="order-456-v1",
        )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
            # Exercise rollback through the public API before the successful commit.
            connection.execute("INSERT INTO orders VALUES (456, 'confirmed')")
            outbox.enqueue(notification, connection=connection)
            connection.rollback()
            assert connection.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)
            assert outbox.get(notification.notification_id) is None

            with connection:
                connection.execute("INSERT INTO orders VALUES (456, 'confirmed')")
                queued = outbox.enqueue(notification, connection=connection)
            assert connection.execute("SELECT COUNT(*) FROM orders").fetchone() == (1,)
        assert queued.created and not attempts

        first = start_worker(database, port)
        pending = outbox.get(notification.notification_id)
        assert first == {"claimed": 1, "delivered": 0, "rescheduled": 1, "dead_lettered": 0}
        assert pending is not None and pending.status is OutboxStatus.PENDING
        assert pending.attempt_count == 1 and pending.last_error_code == "webhook_http_error"
        assert not accepted

        second = start_worker(database, port)
        stored = SQLiteOutbox(database).get(notification.notification_id)
        assert second == {"claimed": 1, "delivered": 1, "rescheduled": 0, "dead_lettered": 0}
        assert stored is not None and stored.status is OutboxStatus.DELIVERED
        assert stored.attempt_count == 2 and stored.provider_status == "202"
        assert stored.provider_id == "order-consumer-v1"
        notification.notification_id = "regenerated-request-id"
        duplicate = SQLiteOutbox(database).enqueue(notification)
        assert not duplicate.created and duplicate.message.message_id == "order-456-v1"

        expected = json.loads(CONTRACT.read_text(encoding="utf-8"))
        assert attempts == [expected, expected]
        assert accepted == [expected]
        return {
            "contract": "order_confirmed_v1",
            "rollback_verified": True,
            "worker_processes": 2,
            "http_attempts": len(attempts),
            "accepted_events": len(accepted),
            "durable_status": stored.status.value,
            "duplicate_created": duplicate.created,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=2, metavar=("DATABASE", "LOOPBACK_PORT"))
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(drain(Path(args.worker[0]), int(args.worker[1])))))
    else:
        with TemporaryDirectory(prefix="samsarix-order-consumer-") as directory:
            print(json.dumps(demonstrate(Path(directory) / "application.sqlite3"), indent=2))


if __name__ == "__main__":
    main()
