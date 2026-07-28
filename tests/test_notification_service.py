from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from helix_notifications import (
    DeliveryError,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    RetryPolicy,
    TransportResult,
)


class ScriptedTransport:
    def __init__(self, outcomes: Sequence[TransportResult | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = 0

    async def send(self, _payload: NotificationPayload) -> TransportResult:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed += 1


class GateTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def send(self, _payload: NotificationPayload) -> TransportResult:
        self.calls += 1
        await self.release.wait()
        return TransportResult(provider_status="accepted")


def payload(**overrides: object) -> NotificationPayload:
    values: dict[str, object] = {
        "channel": "test",
        "recipient": "recipient-1",
        "subject": "Subject",
        "body": "Body",
        "max_retries": 3,
    }
    values.update(overrides)
    return NotificationPayload(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_success_records_truthful_result() -> None:
    transport = ScriptedTransport([TransportResult("provider-1", "202")])
    service = NotificationService(transports={"test": transport})

    result = await service.send(payload(notification_id="notification-1"))

    assert result
    assert result.attempts == 1
    assert result.provider_id == "provider-1"
    assert service.get_delivery("notification-1") == result
    assert service.get_delivery_status("recipient-1") == result.as_dict()
    assert service.failed_notifications == ()


@pytest.mark.asyncio
async def test_retryable_errors_back_off_then_succeed() -> None:
    transport = ScriptedTransport(
        [
            DeliveryError("busy", code="busy", retryable=True),
            DeliveryError("still busy", code="busy", retryable=True),
            TransportResult(provider_status="accepted"),
        ]
    )
    delays: list[float] = []

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    service = NotificationService(
        transports={"test": transport},
        retry_policy=RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=1),
        sleep=capture_sleep,
    )

    result = await service.send(payload())

    assert result.success
    assert result.attempts == 3
    assert delays == [0.5, 1]


@pytest.mark.asyncio
async def test_nonretryable_error_returns_failure_without_retry() -> None:
    transport = ScriptedTransport([DeliveryError("bad request", code="bad", retryable=False)])
    service = NotificationService(transports={"test": transport})

    result = await service.send(payload())

    assert not result
    assert result.error_code == "bad"
    assert result.attempts == 1
    assert transport.calls == 1
    assert service.failed_notifications == (result,)


@pytest.mark.asyncio
async def test_payload_retry_count_limits_remaining_attempts() -> None:
    transport = ScriptedTransport([DeliveryError("busy", code="busy", retryable=True)])
    service = NotificationService(
        transports={"test": transport},
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )

    result = await service.send(payload(retry_count=2, max_retries=3))

    assert not result
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_timeout_is_a_stable_failure() -> None:
    class SlowTransport:
        async def send(self, _payload: NotificationPayload) -> TransportResult:
            await asyncio.sleep(10)
            return TransportResult()

    service = NotificationService(
        transports={"test": SlowTransport()},
        retry_policy=RetryPolicy(max_retries=0, attempt_timeout_seconds=0.01),
    )

    result = await service.send(payload(max_retries=0))

    assert not result
    assert result.error_code == "attempt_timeout"


@pytest.mark.asyncio
async def test_unexpected_transport_error_is_sanitized() -> None:
    transport = ScriptedTransport([RuntimeError("secret detail")])
    service = NotificationService(transports={"test": transport})

    result = await service.send(payload())

    assert result.error_code == "unexpected_transport_error"
    assert "secret detail" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_invalid_transport_result_fails_contract() -> None:
    class InvalidTransport:
        async def send(self, _payload: NotificationPayload) -> object:
            return object()

    service = NotificationService(transports={"test": InvalidTransport()})  # type: ignore[dict-item]

    result = await service.send(payload())

    assert result.error_code == "invalid_transport_result"


@pytest.mark.asyncio
async def test_unsupported_channel_is_not_simulated() -> None:
    service = NotificationService()

    result = await service.send(payload(channel="sms"))

    assert not result
    assert result.attempts == 0
    assert result.error_code == "unsupported_channel"


@pytest.mark.asyncio
async def test_batch_preserves_order_and_duplicate_recipients() -> None:
    transport = ScriptedTransport([TransportResult()])
    service = NotificationService(transports={"test": transport})
    first = payload(notification_id="first")
    second = payload(notification_id="second")

    results = await service.send_batch([first, second])

    assert [result.notification_id for result in results] == ["first", "second"]
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_batch_size_is_bounded_before_tasks_are_created() -> None:
    transport = ScriptedTransport([TransportResult()])
    service = NotificationService(transports={"test": transport}, max_batch_size=1)
    with pytest.raises(NotificationValidationError) as raised:
        await service.send_batch([payload(), payload()])
    assert raised.value.code == "batch_too_large"
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_concurrent_idempotency_sends_once() -> None:
    transport = GateTransport()
    service = NotificationService(transports={"test": transport})
    request = payload(idempotency_key="same-operation")
    first = asyncio.create_task(service.send(request))
    second = asyncio.create_task(service.send(request))
    await asyncio.sleep(0)
    transport.release.set()

    results = await asyncio.gather(first, second)

    assert transport.calls == 1
    assert all(result.success for result in results)
    assert sum(result.deduplicated for result in results) == 1
    assert len(service.delivery_log) == 1
    third = await service.send(request)
    assert third.deduplicated
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_failed_idempotent_delivery_can_be_retried_by_caller() -> None:
    transport = ScriptedTransport(
        [
            DeliveryError("no", code="no"),
            TransportResult(provider_status="accepted"),
        ]
    )
    service = NotificationService(transports={"test": transport})
    request = payload(idempotency_key="retry-me", max_retries=0)

    first = await service.send(request)
    second = await service.send(request)

    assert not first
    assert second
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_history_and_idempotency_caches_are_bounded() -> None:
    transport = ScriptedTransport([TransportResult()])
    service = NotificationService(
        transports={"test": transport}, history_limit=1, idempotency_cache_size=1
    )
    await service.send(payload(notification_id="one", idempotency_key="one"))
    await service.send(payload(notification_id="two", idempotency_key="two"))

    assert [result.notification_id for result in service.delivery_log] == ["two"]
    await service.send(payload(notification_id="one-again", idempotency_key="one"))
    assert transport.calls == 3


@pytest.mark.asyncio
async def test_history_can_be_disabled() -> None:
    service = NotificationService(
        transports={"test": ScriptedTransport([TransportResult()])}, history_limit=0
    )
    result = await service.send(payload())

    assert result
    assert service.delivery_log == ()
    assert service.get_delivery(result.notification_id) is None
    assert service.get_delivery_status(result.recipient) is None


@pytest.mark.asyncio
async def test_context_manager_closes_shared_transport_once() -> None:
    transport = ScriptedTransport([TransportResult()])
    async with NotificationService(transports={"one": transport, "two": transport}) as service:
        assert await service.send(payload(channel="one"))

    assert transport.closed == 1
    result = await service.send(payload(channel="one"))
    assert result.error_code == "service_closed"
    await service.aclose()
    assert transport.closed == 1


def test_configuration_and_payload_validation() -> None:
    with pytest.raises(NotificationValidationError):
        NotificationService(config={"unknown": 1})
    with pytest.raises(NotificationValidationError):
        NotificationService(concurrency_limit=0)
    with pytest.raises(NotificationValidationError):
        NotificationService().register_transport("test", object())  # type: ignore[arg-type]
    with pytest.raises(NotificationValidationError):
        payload(priority="impossible")
    with pytest.raises(NotificationValidationError):
        payload(retry_count=2, max_retries=1)
    with pytest.raises(NotificationValidationError):
        payload(metadata="not-a-map")
    with pytest.raises(NotificationValidationError):
        NotificationService(history_limit=-1)
    with pytest.raises(NotificationValidationError):
        NotificationService(idempotency_cache_size=-1)
    with pytest.raises(NotificationValidationError):
        NotificationService(max_batch_size=0)
    with pytest.raises(NotificationValidationError):
        NotificationService().register_transport("", ScriptedTransport([TransportResult()]))
