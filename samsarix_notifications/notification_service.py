# SPDX-License-Identifier: MPL-2.0
"""Async notification dispatcher with retries, idempotency, and tracking."""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import Any, Protocol

from ._idempotency import snapshot_request
from ._validation import is_bounded_int, is_bounded_number
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
    can use SQLiteOutbox and OutboxWorker or an application-owned durable broker.
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
        max_pending_deliveries: int = 1_000,
        shutdown_timeout_seconds: float = 30.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        values = dict(config or {})
        allowed_config = {
            "concurrency_limit",
            "max_batch_size",
            "history_limit",
            "idempotency_cache_size",
            "max_pending_deliveries",
            "shutdown_timeout_seconds",
        }
        unknown = sorted(set(values) - allowed_config)
        if unknown:
            raise NotificationValidationError(
                f"Unknown notification service configuration keys: {', '.join(unknown)}"
            )
        concurrency_limit = values.get("concurrency_limit", concurrency_limit)
        max_batch_size = values.get("max_batch_size", max_batch_size)
        history_limit = values.get("history_limit", history_limit)
        idempotency_cache_size = values.get("idempotency_cache_size", idempotency_cache_size)
        max_pending_deliveries = values.get("max_pending_deliveries", max_pending_deliveries)
        shutdown_timeout_seconds = values.get("shutdown_timeout_seconds", shutdown_timeout_seconds)
        if not is_bounded_int(concurrency_limit, 1, 1_000):
            raise NotificationValidationError(
                "concurrency_limit must be an integer between 1 and 1000"
            )
        if not is_bounded_int(max_batch_size, 1, 10_000):
            raise NotificationValidationError(
                "max_batch_size must be an integer between 1 and 10000"
            )
        if not is_bounded_int(history_limit, 0, 100_000):
            raise NotificationValidationError(
                "history_limit must be an integer between 0 and 100000"
            )
        if not is_bounded_int(idempotency_cache_size, 0, 100_000):
            raise NotificationValidationError(
                "idempotency_cache_size must be an integer between 0 and 100000"
            )
        if not is_bounded_int(max_pending_deliveries, 1, 100_000):
            raise NotificationValidationError(
                "max_pending_deliveries must be an integer between 1 and 100000"
            )
        if not is_bounded_number(shutdown_timeout_seconds, 0, 300, exclusive_minimum=True):
            raise NotificationValidationError("shutdown_timeout_seconds must be between 0 and 300")

        self.retry_policy = retry_policy or RetryPolicy()
        self._max_batch_size = max_batch_size
        self._transports: dict[str, NotificationTransport] = {}
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._history: deque[DeliveryResult] | None = (
            deque(maxlen=history_limit) if history_limit else None
        )
        self._idempotency_cache_size = idempotency_cache_size
        self._completed: OrderedDict[str, tuple[bytes, DeliveryResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[DeliveryResult]] = {}
        self._inflight_fingerprints: dict[str, bytes] = {}
        self._active: set[asyncio.Task[DeliveryResult]] = set()
        self._max_pending_deliveries = max_pending_deliveries
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._close_task: asyncio.Task[None] | None = None
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

        if self._closed:
            raise NotificationValidationError(
                "Cannot register a transport after shutdown starts", code="service_closed"
            )
        channel_name = _channel_name(channel)
        if not callable(getattr(transport, "send", None)):
            raise NotificationValidationError("transport must define an async send method")
        self._transports[channel_name] = transport

    async def send(self, payload: NotificationPayload) -> DeliveryResult:
        """Send one notification and always return a truthful final result."""

        return await self._submit(payload, deduplicate=True)

    async def _send_without_retries(self, payload: NotificationPayload) -> DeliveryResult:
        """Send one unchanged payload with the dispatcher's retry loop disabled."""

        return await self._submit(payload, deduplicate=False, maximum_retries=0)

    @property
    def pending_deliveries(self) -> int:
        """Number of accepted deliveries running or waiting for a transport slot."""

        return len(self._active)

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
        """Stop admissions, drain accepted work, then close unique transports.

        Each drain/cleanup phase has the configured shutdown grace period.
        Custom transports must cooperate with asyncio cancellation.
        """

        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._drain_and_close())
            self._close_task.add_done_callback(_observe_close_exception)
        # Cancelling a shutdown waiter must not interrupt shared cleanup.
        await asyncio.shield(self._close_task)

    async def _drain_and_close(self) -> None:
        # Publish the shared close task before invoking any user-defined closer,
        # including when the host opts into an eager asyncio task factory.
        await asyncio.sleep(0)
        timed_out = False
        if self._active:
            _, pending = await asyncio.wait(
                tuple(self._active), timeout=self._shutdown_timeout_seconds
            )
            if pending:
                timed_out = True
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        closers: list[asyncio.Task[None]] = []
        seen: set[int] = set()
        for transport in self._transports.values():
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            closers.append(asyncio.create_task(_close_transport(transport)))
        close_failed = False
        if closers:
            _, pending_closers = await asyncio.wait(closers, timeout=self._shutdown_timeout_seconds)
            if pending_closers:
                timed_out = True
                for closer in pending_closers:
                    closer.cancel()
            outcomes = await asyncio.gather(*closers, return_exceptions=True)
            close_failed = any(isinstance(outcome, BaseException) for outcome in outcomes)
        self._completed.clear()
        if timed_out:
            raise NotificationError(
                "Notification shutdown grace period elapsed; in-progress outcomes may be ambiguous",
                code="shutdown_timeout",
            )
        if close_failed:
            raise NotificationError(
                "One or more notification transports failed to close", code="transport_close_failed"
            )

    async def __aenter__(self) -> NotificationService:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _submit(
        self,
        payload: NotificationPayload,
        *,
        deduplicate: bool,
        maximum_retries: int | None = None,
    ) -> DeliveryResult:
        if not isinstance(payload, NotificationPayload):
            raise NotificationValidationError("payload must be a NotificationPayload")
        if self._closed:
            return self._failure_result(
                payload,
                started_at=utc_now(),
                attempts=0,
                code="service_closed",
                message="Notification service is closed",
                retryable=False,
            )
        key = payload.idempotency_key if deduplicate and self._idempotency_cache_size else None
        fingerprint = None
        if key is not None:
            try:
                payload, fingerprint = snapshot_request(payload)
            except NotificationValidationError as exc:
                return self._reject_submission(payload, code=exc.code, message=str(exc))
            key = payload.idempotency_key
        # Admission and task registration contain no await: one event loop owns
        # this service, so duplicate lookup and capacity reservation are atomic.
        if key is not None:
            completed = self._completed.get(key)
            if completed is not None:
                if completed[0] != fingerprint:
                    return self._idempotency_conflict(payload)
                self._completed.move_to_end(key)
                return replace(completed[1], deduplicated=True)
            task = self._inflight.get(key)
            if task is not None:
                if self._inflight_fingerprints[key] != fingerprint:
                    return self._idempotency_conflict(payload)
                return replace(await asyncio.shield(task), deduplicated=True)
        if len(self._active) >= self._max_pending_deliveries:
            result = self._failure_result(
                payload,
                started_at=utc_now(),
                attempts=0,
                code="service_busy",
                message="Notification service pending-delivery capacity reached",
                retryable=True,
            )
            self._record(result)
            return result
        task = asyncio.create_task(self._execute(payload, maximum_retries=maximum_retries))
        self._active.add(task)
        if key is not None:
            self._inflight[key] = task
            assert fingerprint is not None
            self._inflight_fingerprints[key] = fingerprint
        # Cleanup belongs to the delivery, not to a possibly cancelled waiter.
        # A done callback also handles cancellation before the coroutine starts.
        task.add_done_callback(partial(self._delivery_done, key=key, fingerprint=fingerprint))
        if key is not None:
            return await asyncio.shield(task)
        return await task

    def _idempotency_conflict(self, payload: NotificationPayload) -> DeliveryResult:
        return self._reject_submission(
            payload,
            code="idempotency_conflict",
            message="Idempotency key was reused for a different notification request",
        )

    def _reject_submission(
        self, payload: NotificationPayload, *, code: str, message: str
    ) -> DeliveryResult:
        result = self._failure_result(
            payload, started_at=utc_now(), attempts=0, code=code, message=message, retryable=False
        )
        self._record(result)
        return result

    def _delivery_done(
        self, task: asyncio.Task[DeliveryResult], *, key: str | None, fingerprint: bytes | None
    ) -> None:
        self._active.discard(task)
        if key is not None and self._inflight.get(key) is task:
            self._inflight.pop(key)
            self._inflight_fingerprints.pop(key)
        if task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if key is not None and result.success:
            assert fingerprint is not None
            self._completed[key] = (fingerprint, result)
            self._completed.move_to_end(key)
            while len(self._completed) > self._idempotency_cache_size:
                self._completed.popitem(last=False)

    async def _execute(
        self,
        payload: NotificationPayload,
        *,
        maximum_retries: int | None = None,
    ) -> DeliveryResult:
        # Eager task factories can run a coroutine inside create_task. Yield
        # before invoking user code so admission/task ownership is registered.
        await asyncio.sleep(0)
        started_at = utc_now()
        transport = self._transports.get(str(payload.channel))
        if transport is None:
            result = self._failure_result(
                payload,
                started_at=started_at,
                attempts=0,
                code="unsupported_channel",
                message=f"No transport is registered for channel {payload.channel}",
                retryable=False,
            )
            self._record(result)
            return result

        available_retries = max(0, payload.max_retries - payload.retry_count)
        max_retries = min(available_retries, self.retry_policy.max_retries)
        if maximum_retries is not None:
            max_retries = min(max_retries, maximum_retries)
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
            retryable=last_error.retryable,
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
        retryable: bool,
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
            retryable=retryable,
        )

    def _record(self, result: DeliveryResult) -> None:
        if self._history is not None:
            self._history.append(result)


def _channel_name(channel: str | NotificationChannel) -> str:
    value = channel.value if isinstance(channel, NotificationChannel) else channel
    if not isinstance(value, str) or not value.strip():
        raise NotificationValidationError("channel must be a non-empty string")
    return value.strip().lower()


async def _close_transport(transport: NotificationTransport) -> None:
    closer = getattr(transport, "aclose", None)
    if closer is not None:
        outcome = closer()
        if inspect.isawaitable(outcome):
            await outcome


def _observe_close_exception(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
