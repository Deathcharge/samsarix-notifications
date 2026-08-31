# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import ClassVar

import pytest

from samsarix_notifications import (
    NotificationPayload,
    NotificationService,
    WebhookPolicy,
    WebhookRouter,
)


class Receiver(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.received.append(json.loads(self.rfile.read(length)))
        self.send_response(202)
        self.send_header("X-Request-ID", "integration-test")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.mark.asyncio
async def test_complete_local_webhook_journey() -> None:
    Receiver.received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    router = WebhookRouter(
        policy=WebhookPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port}),
            allow_private_addresses=True,
        )
    )
    request = NotificationPayload(
        channel="webhook",
        recipient=f"http://127.0.0.1:{port}/notifications",
        subject="Integration",
        body="It works",
        idempotency_key="integration-1",
    )
    try:
        async with NotificationService(transports={"webhook": router}) as service:
            result = await service.send(request)
        assert result.success
        assert result.provider_status == "202"
        assert result.provider_id == "integration-test"
        assert Receiver.received == [
            {
                "id": request.notification_id,
                "event": "notification",
                "data": {
                    "subject": "Integration",
                    "body": "It works",
                    "priority": "normal",
                    "metadata": {},
                },
            }
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_cancelled_request_then_shutdown_preserves_real_http_acknowledgement() -> None:
    started = Event()
    release = Event()
    events: list[dict[str, object]] = []

    class SlowReceiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.connection.settimeout(3)
            length = int(self.headers["Content-Length"])
            events.append(json.loads(self.rfile.read(length)))
            started.set()
            status = 202 if release.wait(timeout=3) else 503
            self.send_response(status)
            self.send_header("X-Request-ID", "late-provider-acceptance")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowReceiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    router = WebhookRouter(
        policy=WebhookPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port}),
            allow_private_addresses=True,
        )
    )
    service = NotificationService(transports={"webhook": router}, shutdown_timeout_seconds=2)
    request = NotificationPayload(
        channel="webhook",
        recipient=f"http://127.0.0.1:{port}/notifications",
        subject="Order confirmed",
        body="The request handler disconnected.",
        idempotency_key="disconnected-order",
        notification_id="order-cancellation-1",
    )
    sending = asyncio.create_task(service.send(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert service.pending_deliveries == 1
        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 3)
        delivered = service.get_delivery(request.notification_id)
        assert delivered is not None and delivered.success
        assert delivered.provider_id == "late-provider-acceptance"
        assert delivered.provider_status == "202"
        assert service.pending_deliveries == 0
        assert len(events) == 1 and events[0]["id"] == request.notification_id
    finally:
        release.set()
        if not sending.done():
            sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await service.aclose()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
