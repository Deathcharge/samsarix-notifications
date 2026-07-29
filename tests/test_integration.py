# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
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
