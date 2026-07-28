"""Run a credential-free notification journey against a localhost receiver."""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

from helix_notifications import (
    NotificationPayload,
    NotificationService,
    WebhookPolicy,
    WebhookRouter,
)


class Receiver(BaseHTTPRequestHandler):
    """Minimal local JSON receiver used only by this example."""

    received: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers["Content-Length"])
        self.received.append(json.loads(self.rfile.read(length)))
        self.send_response(202)
        self.send_header("X-Request-ID", "local-demo")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    policy = WebhookPolicy(
        allowed_schemes=frozenset({"http"}),
        allowed_hosts=frozenset({"127.0.0.1"}),
        allowed_ports=frozenset({port}),
        allow_private_addresses=True,
    )
    webhook = WebhookRouter(policy=policy)
    payload = NotificationPayload(
        channel="webhook",
        recipient=f"http://127.0.0.1:{port}/notifications",
        subject="Local example",
        body="The complete delivery journey worked.",
        idempotency_key="local-example-1",
    )
    try:
        async with NotificationService(transports={"webhook": webhook}) as service:
            result = await service.send(payload)
        print(json.dumps(result.as_dict(), indent=2))
        print(json.dumps(Receiver.received[0], indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    asyncio.run(main())
