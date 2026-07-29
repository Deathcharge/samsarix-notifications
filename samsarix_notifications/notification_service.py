# SPDX-License-Identifier: MPL-2.0
"""Async notification dispatcher with retries, idempotency, and tracking."""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol

from .errors import DeliveryError, NotificationError, NotificationValidationError
from .models import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    NotificationPayload,
    RetryPolicy,
    TransportResult,
    utc_now,
)


class NotificationTransport(Protocol):
    """Minimal interface implemented by built-in and custom transports."""

    async def send(self, payload: NotificationPayload) -> TransportResult:
        """Attempt one delivery or raise a typed ``NotificationError``."""


Sleep = Callable[[float], Awaitable[None]]


class NotificationService:
    """Dispatch notifications through registered async transports.

    Delivery history and idempotency are deliberately process-local and bounded.
    Applications that require crash-safe queues or cross-process deduplication
    should persist requests before calling this library.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        transports: Mapping[str | NotificationChannel, NotificationTransport] | None = None,
        retry_policy: RetryPolicy | None = None,
        concurrency_limit: int = 10,
        max_batch_size: int = 1_000,
        history_limit: int = 1_000,
        idempotency_cache_size: int = 1_000,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        values = dict(config or {})
        allowed_config = {
            "concurrency_limit",
            "max_batch_size",
            "history_limit",
            "idempotency_cache_size",
        }
        unknown = sorted(set(values) - allowed_config)
        if unknown:
            raise NotificationValidationError(
                f"Unknown notification service configuration keys: {', '.join(unknown)}"
            )
        concurrency_limit = int(values.get("concurrency_limit", concurrency_limit))
        max_batch_size = int(values.get("max_batch_size", max_batch_size))
        history_limit = int(values.get("history_limit", history_limit))
        idempotency_cache_size = int(values.get("idempotency_cache_size", idempotency_cache_size))
        if not 1 <= concurrency_limit <= 1_000:
            raise NotificationValidationError("concurrency_limit must be between 1 and 1000")
        if not 1 <= max_batch_size <= 10_000:
            raise NotificationValidationError("max_batch_size must be between 1 and 10000")
        if not 0 <= history_limit <= 100_000:
            raise NotificationValidationError("history_limit must be between 0 and 100000")
        if not 0 <= idempotency_cache_size <= 100_000:
            raise NotificationValidationError("idempotency_cache_size must be between 0 and 100000")

        self.retry_policy = retry_policy or RetryPolicy()
        self._max_batch_size = max_batch_size
        self._transports: dict[str, NotificationTransport] = {}
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._history: deque[DeliveryResult] | None = (
            deque(maxlen=history_limit) if history_limit else None
        )
        self._idempotency_cache_size = idempotency_cache_size
        self._completed: OrderedDict[str, DeliveryResult] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[DeliveryResult]] = {}
        self._idempotency_lock = asyncio.Lock()
        self._sleep = sleep
        self._closed = False
        for channel, transport in (transports or {}).items():
            self.register_transport(channel, transport)

    def register_transport(
        self,
        channel: str | NotificationChannel,
        transport: NotificationTransport,
    ) -> None:
        """Register or replace one channel transport."""

        channel_name = _channel_name(channel)
        if not callable(getattr(transport, "send", None)):
            raise NotificationValidationError("transport must define an async send method")
        self._transports[channel_name] = transport

    async def send(self, payload: NotificationPayload) -> DeliveryResult:
        """Send one notification and always return a truthful final result."""

        if not isinstance(payload, NotificationPayload):
            raise NotificationValidationError("payload must be a NotificationPayload")
        if self._closed:
            return self._failure_result(
                payload,
                started_at=utc_now(),
                attempts=0,
                code="service_closed",
                message="Notification service is closed",
            )
        if payload.idempotency_key is None or self._idempotency_cache_size == 0:
            return await self._execute(payload)
        return await self._send_idempotent(payload)

    async def send_batch(
        self,
        payloads: Sequence[NotificationPayload],
    ) -> list[DeliveryResult]:
        """Send a bounded-concurrency batch while preserving input order."""

        if len(payloads) > self._max_batch_size:
            raise NotificationValidationError(
                f"batch size exceeds configured maximum of {self._max_batch_size}",
                code="batch_too_large",
            )
        return list(await asyncio.gather(*(self.send(payload) for payload in payloads)))

    def get_delivery(self, notification_id: str) -> DeliveryResult | None:
        """Return the newest retained result for a notification ID."""

        if self._history is None:
            return None
        for result in reversed(self._history):
            if result.notification_id == notification_id:
                return result
        return None

    def get_delivery_status(self, recipient: str) -> dict[str, Any] | None:
        """Return the newest retained result for a recipient as a dictionary."""

        if self._history is None:
            return None
        for result in reversed(self._history):
            if result.recipient == recipient:
                return result.as_dict()
        return None

    @property
    def delivery_log(self) -> tuple[DeliveryResult, ...]:
        """A snapshot of bounded in-memory delivery history."""

        return tuple(self._history or ())

    @property
    def failed_notifications(self) -> tuple[DeliveryResult, ...]:
        """Retained failed results without storing message bodies."""

        return tuple(result for result in self.delivery_log if not result.success)

    async def aclose(self) -> None:
        """Close each unique transport that exposes ``aclose``."""

        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for transport in self._transports.values():
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            closer = getattr(transport, "aclose", None)
            if closer is not None:
                outcome = closer()
                if inspect.isawaitable(outcome):
                    await outcome

    async def __aenter__(self) -> NotificationService:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _send_idempotent(self, payload: NotificationPayload) -> DeliveryResult:
        assert payload.idempotency_key is not None
        key = payload.idempotency_key
        async with self._idempotency_lock:
            completed = self._completed.get(key)
            if completed is not None:
                self._completed.move_to_end(key)
                return replace(completed, deduplicated=True)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._execute(payload))
                self._inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._idempotency_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        if result.success:
            async with self._idempotency_lock:
                existing = self._completed.get(key)
                if existing is None:
                    self._completed[key] = result
                    while len(self._completed) > self._idempotency_cache_size:
                        self._completed.popitem(last=False)
                    return result
                return replace(existing, deduplicated=True)
        return result

    async def _execute(self, payload: NotificationPayload) -> DeliveryResult:
        started_at = utc_now()
        transport = self._transports.get(str(payload.channel))
        if transport is None:
            result = self._failure_result(
                payload,
                started_at=started_at,
                attempts=0,
                code="unsupported_channel",
                message=f"No transport is registered for channel {payload.channel}",
            )
            self._record(result)
            return result

        available_retries = max(0, payload.max_retries - payload.retry_count)
        max_retries = min(available_retries, self.retry_policy.max_retries)
        last_error = DeliveryError("Delivery failed", code="delivery_failed")
        attempts = 0
        async with self._semaphore:
            for attempt in range(max_retries + 1):
                attempts = attempt + 1
                try:
                    transport_result = await asyncio.wait_for(
                        transport.send(payload),
                        timeout=self.retry_policy.attempt_timeout_seconds,
                    )
                    if not isinstance(transport_result, TransportResult):
                        raise DeliveryError(
                            "Transport returned an invalid result",
                            code="invalid_transport_result",
                        )
                    result = DeliveryResult(
                        notification_id=payload.notification_id,
                        channel=str(payload.channel),
                        recipient=payload.recipient,
                        status=DeliveryStatus.DELIVERED,
                        attempts=attempts,
                        started_at=started_at,
                        completed_at=utc_now(),
                        provider_id=transport_result.provider_id,
                        provider_status=transport_result.provider_status,
                    )
                    self._record(result)
                    return result
                except asyncio.TimeoutError:
                    last_error = DeliveryError(
                        "Transport attempt timed out",
                        code="attempt_timeout",
                        retryable=True,
                    )
                except NotificationError as exc:
                    last_error = DeliveryError(str(exc), code=exc.code, retryable=exc.retryable)
                except Exception:
                    last_error = DeliveryError(
                        "Transport raised an unexpected error",
                        code="unexpected_transport_error",
                        retryable=False,
                    )
                if not last_error.retryable or attempt >= max_retries:
                    break
                await self._sleep(self.retry_policy.delay_before_retry(attempt + 1))

        result = self._failure_result(
            payload,
            started_at=started_at,
            attempts=attempts,
            code=last_error.code,
            message=str(last_error),
        )
        self._record(result)
        return result

    def _failure_result(
        self,
        payload: NotificationPayload,
        *,
        started_at: datetime,
        attempts: int,
        code: str,
        message: str,
    ) -> DeliveryResult:
        return DeliveryResult(
            notification_id=payload.notification_id,
            channel=str(payload.channel),
            recipient=payload.recipient,
            status=DeliveryStatus.FAILED,
            attempts=attempts,
            started_at=started_at,
            completed_at=utc_now(),
            error_code=code,
            error_message=message,
        )

    def _record(self, result: DeliveryResult) -> None:
        if self._history is not None:
            self._history.append(result)


def _channel_name(channel: str | NotificationChannel) -> str:
    value = channel.value if isinstance(channel, NotificationChannel) else channel
    if not isinstance(value, str) or not value.strip():
        raise NotificationValidationError("channel must be a non-empty string")
    return value.strip().lower()
