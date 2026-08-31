# SPDX-License-Identifier: MPL-2.0
"""HTTP webhook delivery with destination policy and optional HMAC signing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import SplitResult, urlsplit
from uuid import uuid4

import httpx

from ._json import encode_json
from .errors import ConfigurationError, DeliveryError, NotificationValidationError
from .models import NotificationPayload, TransportResult

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class WebhookPolicy:
    """Security and resource limits for outbound webhooks."""

    allowed_schemes: frozenset[str] = field(default_factory=lambda: frozenset({"https"}))
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    allowed_ports: frozenset[int] = field(default_factory=lambda: frozenset({443}))
    allow_private_addresses: bool = False
    max_payload_bytes: int = 256 * 1024
    timeout_seconds: float = 10.0
    max_connections: int = 20

    def __post_init__(self) -> None:
        schemes = frozenset(value.lower() for value in self.allowed_schemes)
        if not schemes or not schemes <= {"http", "https"}:
            raise ConfigurationError("Webhook schemes must contain http and/or https")
        object.__setattr__(self, "allowed_schemes", schemes)
        normalized_hosts = frozenset(_normalize_host(value) for value in self.allowed_hosts)
        object.__setattr__(self, "allowed_hosts", normalized_hosts)
        if any(not 1 <= port <= 65_535 for port in self.allowed_ports):
            raise ConfigurationError("Webhook ports must be between 1 and 65535")
        if not 1_024 <= self.max_payload_bytes <= 10 * 1024 * 1024:
            raise ConfigurationError("Webhook max_payload_bytes must be between 1 KiB and 10 MiB")
        if not 0 < self.timeout_seconds <= 300:
            raise ConfigurationError("Webhook timeout_seconds must be between 0 and 300")
        if not 1 <= self.max_connections <= 1_000:
            raise ConfigurationError("Webhook max_connections must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class WebhookRoute:
    """A registered event destination and optional signing secret."""

    destination: str
    secret: bytes | None = field(default=None, repr=False)


class WebhookRouter:
    """Route JSON events to bounded, validated HTTP destinations."""

    def __init__(
        self,
        *,
        policy: WebhookPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.policy = policy or WebhookPolicy()
        self.routes: dict[str, WebhookRoute] = {}
        self._resolver = resolver or _resolve_addresses
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self.policy.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=self.policy.max_connections,
                max_keepalive_connections=self.policy.max_connections,
            ),
        )

    def register_route(
        self,
        event_type: str,
        destination: str,
        *,
        secret: str | bytes | None = None,
    ) -> None:
        """Register a route after synchronous URL-policy validation."""

        _validate_event_type(event_type)
        self._parse_destination(destination)
        secret_bytes = secret.encode() if isinstance(secret, str) else secret
        if secret_bytes is not None and len(secret_bytes) < 16:
            raise ConfigurationError("Webhook signing secrets must be at least 16 bytes")
        self.routes[event_type] = WebhookRoute(destination, secret_bytes)

    async def route(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
    ) -> TransportResult:
        """Deliver a registered event route."""

        _validate_event_type(event_type)
        try:
            route = self.routes[event_type]
        except KeyError as exc:
            raise ConfigurationError(
                f"No webhook route is registered for event type {event_type}",
                code="unknown_webhook_route",
            ) from exc
        body = _encode_json(
            {
                "id": event_id or str(uuid4()),
                "event": event_type,
                "data": dict(payload),
            },
            max_bytes=self.policy.max_payload_bytes,
        )
        return await self._deliver(route, event_type, body)

    async def send(self, payload: NotificationPayload) -> TransportResult:
        """Use a notification payload's recipient as a direct webhook URL."""

        event_type_value = payload.metadata.get("event_type", "notification")
        if not isinstance(event_type_value, str):
            raise NotificationValidationError("webhook event_type metadata must be a string")
        _validate_event_type(event_type_value)
        metadata = dict(payload.metadata)
        metadata.pop("event_type", None)
        body = _encode_json(
            {
                "id": payload.notification_id,
                "event": event_type_value,
                "data": {
                    "subject": payload.subject,
                    "body": payload.body,
                    "priority": payload.priority,
                    "metadata": metadata,
                },
            },
            max_bytes=self.policy.max_payload_bytes,
        )
        return await self._deliver(WebhookRoute(payload.recipient), event_type_value, body)

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> WebhookRouter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _deliver(
        self,
        route: WebhookRoute,
        event_type: str,
        body: bytes,
    ) -> TransportResult:
        if len(body) > self.policy.max_payload_bytes:
            raise NotificationValidationError(
                "encoded webhook payload exceeds max_payload_bytes",
                code="webhook_payload_too_large",
            )
        await self._validate_destination(route.destination)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "samsarix-notifications/0.1",
            "X-Samsarix-Event": event_type,
        }
        if route.secret is not None:
            digest = hmac.new(route.secret, body, hashlib.sha256).hexdigest()
            headers["X-Samsarix-Signature"] = f"sha256={digest}"
        try:
            async with self._client.stream(
                "POST",
                route.destination,
                content=body,
                headers=headers,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                provider_id = response.headers.get("X-Request-ID")
        except httpx.TimeoutException as exc:
            raise DeliveryError(
                "Webhook request timed out", code="webhook_timeout", retryable=True
            ) from exc
        except httpx.NetworkError as exc:
            raise DeliveryError(
                "Webhook connection failed", code="webhook_network_error", retryable=True
            ) from exc
        if not 200 <= status < 300:
            retryable = status in {408, 425, 429} or status >= 500
            raise DeliveryError(
                f"Webhook destination returned HTTP {status}",
                code="webhook_http_error",
                retryable=retryable,
            )
        if provider_id is not None:
            provider_id = provider_id[:128]
        return TransportResult(provider_id=provider_id, provider_status=str(status))

    async def _validate_destination(self, destination: str) -> None:
        parsed, host, port = self._parse_destination(destination)
        del parsed
        try:
            address = ipaddress.ip_address(host)
            addresses = [address]
        except ValueError:
            try:
                resolved = await self._resolver(host, port)
            except (OSError, socket.gaierror) as exc:
                raise DeliveryError(
                    "Webhook destination DNS lookup failed",
                    code="webhook_dns_error",
                    retryable=True,
                ) from exc
            if not resolved:
                raise DeliveryError(
                    "Webhook destination DNS lookup returned no addresses",
                    code="webhook_dns_error",
                    retryable=True,
                ) from None
            try:
                addresses = [ipaddress.ip_address(value) for value in resolved]
            except ValueError as exc:
                raise DeliveryError(
                    "Webhook destination resolved to an invalid address",
                    code="webhook_dns_error",
                ) from exc
        if not self.policy.allow_private_addresses and any(
            not address.is_global for address in addresses
        ):
            raise DeliveryError(
                "Webhook destination resolves to a non-public address",
                code="webhook_private_destination",
            )

    def _parse_destination(self, destination: str) -> tuple[SplitResult, str, int]:
        if not isinstance(destination, str) or len(destination) > 2_048:
            raise NotificationValidationError("webhook destination must be a URL")
        parsed = urlsplit(destination)
        scheme = parsed.scheme.lower()
        if scheme not in self.policy.allowed_schemes:
            raise DeliveryError(
                "Webhook destination scheme is not allowed",
                code="webhook_scheme_not_allowed",
            )
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise DeliveryError(
                "Webhook destination must have a hostname and no embedded credentials",
                code="invalid_webhook_destination",
            )
        if parsed.fragment:
            raise DeliveryError(
                "Webhook destination fragments are not allowed",
                code="invalid_webhook_destination",
            )
        host = _normalize_host(parsed.hostname)
        if host not in self.policy.allowed_hosts:
            raise DeliveryError(
                "Webhook destination host is not allowlisted",
                code="webhook_host_not_allowed",
            )
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise DeliveryError(
                "Webhook destination port is invalid", code="invalid_webhook_destination"
            ) from exc
        if self.policy.allowed_ports and port not in self.policy.allowed_ports:
            raise DeliveryError(
                "Webhook destination port is not allowed",
                code="webhook_port_not_allowed",
            )
        return parsed, host, port


async def _resolve_addresses(host: str, port: int) -> Sequence[str]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    return sorted({str(record[4][0]) for record in records})


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host or "%" in host:
        raise ConfigurationError("Webhook host is invalid")
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConfigurationError("Webhook host is invalid") from exc


def _validate_event_type(event_type: str) -> None:
    if not isinstance(event_type, str) or _EVENT_TYPE.fullmatch(event_type) is None:
        raise NotificationValidationError(
            "event_type must match [A-Za-z0-9_.:-] and contain 1 to 128 characters"
        )


def _encode_json(value: Mapping[str, Any], *, max_bytes: int) -> bytes:
    return encode_json(value, max_bytes=max_bytes, prefix="webhook").encode("utf-8")
