from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence

import httpx
import pytest

from helix_notifications import (
    ConfigurationError,
    DeliveryError,
    NotificationPayload,
    NotificationValidationError,
    WebhookPolicy,
    WebhookRouter,
)


async def public_resolver(_host: str, _port: int) -> Sequence[str]:
    return ["93.184.216.34"]


def allowed_policy(host: str = "example.com", **overrides: object) -> WebhookPolicy:
    values: dict[str, object] = {"allowed_hosts": frozenset({host})}
    values.update(overrides)
    return WebhookPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_direct_notification_sends_json_without_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, headers={"X-Request-ID": "provider-123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = WebhookRouter(client=client, resolver=public_resolver, policy=allowed_policy())
        payload = NotificationPayload(
            channel="webhook",
            recipient="https://example.com/hook",
            subject="Subject",
            body="Body",
            metadata={"event_type": "build.finished", "build": 42},
            notification_id="notification-1",
        )

        result = await router.send(payload)

    assert result.provider_status == "202"
    assert result.provider_id == "provider-123"
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["X-Helix-Event"] == "build.finished"
    assert json.loads(request.content) == {
        "id": "notification-1",
        "event": "build.finished",
        "data": {
            "subject": "Subject",
            "body": "Body",
            "priority": "normal",
            "metadata": {"build": 42},
        },
    }


@pytest.mark.asyncio
async def test_registered_route_signs_exact_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    secret = b"a-secret-with-16-bytes"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = WebhookRouter(client=client, resolver=public_resolver, policy=allowed_policy())
        router.register_route("alert.created", "https://example.com/alerts", secret=secret)
        result = await router.route("alert.created", {"severity": "high"}, event_id="event-1")

    assert result.provider_status == "204"
    expected = hmac.new(secret, requests[0].content, hashlib.sha256).hexdigest()
    assert requests[0].headers["X-Helix-Signature"] == f"sha256={expected}"


@pytest.mark.asyncio
async def test_private_destination_is_rejected_before_request() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async def private_resolver(_host: str, _port: int) -> Sequence[str]:
        return ["127.0.0.1"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = WebhookRouter(
            client=client,
            resolver=private_resolver,
            policy=allowed_policy("internal.example"),
        )
        with pytest.raises(ConfigurationError) as raised:
            await router.route("event", {})
        assert raised.value.code == "unknown_webhook_route"
        router.register_route("event", "https://internal.example/hook")
        with pytest.raises(DeliveryError) as raised:
            await router.route("event", {})
    assert raised.value.code == "webhook_private_destination"
    assert not called


@pytest.mark.asyncio
async def test_http_error_retry_classification() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = WebhookRouter(client=client, resolver=public_resolver, policy=allowed_policy())
        router.register_route("event", "https://example.com/hook")
        with pytest.raises(DeliveryError) as raised:
            await router.route("event", {})
    assert raised.value.code == "webhook_http_error"
    assert raised.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("slow"), "webhook_timeout"),
        (httpx.ConnectError("offline"), "webhook_network_error"),
    ],
)
async def test_network_errors_are_typed(error: Exception, code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = WebhookRouter(client=client, resolver=public_resolver, policy=allowed_policy())
        router.register_route("event", "https://example.com/hook")
        with pytest.raises(DeliveryError) as raised:
            await router.route("event", {})
    assert raised.value.code == code
    assert raised.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [[], ["not-an-ip"]])
async def test_dns_failures_are_typed(resolved: Sequence[str]) -> None:
    async def resolver(_host: str, _port: int) -> Sequence[str]:
        return resolved

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        router = WebhookRouter(client=client, resolver=resolver, policy=allowed_policy())
        router.register_route("event", "https://example.com/hook")
        with pytest.raises(DeliveryError) as raised:
            await router.route("event", {})
    assert raised.value.code == "webhook_dns_error"


@pytest.mark.asyncio
async def test_dns_exception_is_retryable() -> None:
    async def resolver(_host: str, _port: int) -> Sequence[str]:
        raise OSError("dns offline")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        router = WebhookRouter(client=client, resolver=resolver, policy=allowed_policy())
        router.register_route("event", "https://example.com/hook")
        with pytest.raises(DeliveryError) as raised:
            await router.route("event", {})
    assert raised.value.code == "webhook_dns_error"
    assert raised.value.retryable


@pytest.mark.asyncio
async def test_non_json_and_oversized_payloads_are_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        router = WebhookRouter(
            client=client,
            resolver=public_resolver,
            policy=allowed_policy(max_payload_bytes=1_024),
        )
        router.register_route("event", "https://example.com/hook")
        with pytest.raises(NotificationValidationError) as raised:
            await router.route("event", {"not_json": object()})
        assert raised.value.code == "webhook_payload_not_json"
        with pytest.raises(NotificationValidationError) as raised:
            await router.route("event", {"body": "x" * 2_000})
        assert raised.value.code == "webhook_payload_too_large"


def test_destination_policy_validation() -> None:
    router = WebhookRouter(policy=allowed_policy())
    with pytest.raises(DeliveryError, match="scheme"):
        router.register_route("event", "http://example.com/hook")
    with pytest.raises(DeliveryError, match="credentials"):
        router.register_route("event", "https://user:pass@example.com/hook")
    with pytest.raises(DeliveryError, match="fragments"):
        router.register_route("event", "https://example.com/hook#fragment")
    with pytest.raises(DeliveryError, match="allowlisted"):
        WebhookRouter(
            policy=WebhookPolicy(allowed_hosts=frozenset({"allowed.example"}))
        ).register_route("event", "https://other.example/hook")
    with pytest.raises(DeliveryError, match="port"):
        router.register_route("event", "https://example.com:8443/hook")
    with pytest.raises(NotificationValidationError):
        router.register_route("not an event", "https://example.com/hook")
    with pytest.raises(ConfigurationError, match="at least 16"):
        router.register_route("event", "https://example.com/hook", secret="short")
    with pytest.raises(DeliveryError, match="port"):
        router.register_route("event", "https://example.com:bad/hook")
    with pytest.raises(NotificationValidationError):
        router.register_route("event", 1)  # type: ignore[arg-type]
    with pytest.raises(DeliveryError, match="allowlisted"):
        WebhookRouter().register_route("event", "https://example.com/hook")


def test_policy_validation() -> None:
    with pytest.raises(ConfigurationError):
        WebhookPolicy(allowed_schemes=frozenset({"ftp"}))
    with pytest.raises(ConfigurationError):
        WebhookPolicy(allowed_ports=frozenset({0}))
    with pytest.raises(ConfigurationError):
        WebhookPolicy(max_connections=0)
    with pytest.raises(ConfigurationError):
        WebhookPolicy(max_payload_bytes=1)
    with pytest.raises(ConfigurationError):
        WebhookPolicy(timeout_seconds=0)


@pytest.mark.asyncio
async def test_owned_client_context_and_invalid_event_metadata() -> None:
    router = WebhookRouter()
    async with router as entered:
        assert entered is router
    assert router._client.is_closed
    invalid = NotificationPayload(
        "webhook", "https://example.com", "Subject", "Body", metadata={"event_type": 1}
    )
    async with WebhookRouter() as invalid_router:
        with pytest.raises(NotificationValidationError, match="event_type"):
            await invalid_router.send(invalid)
