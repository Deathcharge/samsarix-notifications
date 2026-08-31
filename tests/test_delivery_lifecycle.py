# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import asyncio

import pytest

from samsarix_notifications import (
    DeliveryError,
    NotificationError,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    TransportResult,
)


class LifecycleTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed = 0
        self.calls = 0

    async def send(self, _payload: NotificationPayload) -> TransportResult:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
            assert self.closed == 0, "transport closed before accepted delivery completed"
            return TransportResult(provider_status="accepted")
        finally:
            self.finished.set()

    async def aclose(self) -> None:
        self.closed += 1


def request(key: str | None = None) -> NotificationPayload:
    return NotificationPayload(
        channel="test",
        recipient="customer-456",
        subject="Order confirmed",
        body="Order 456 has been confirmed.",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_cancelled_idempotent_waiter_does_not_retain_finished_task() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport})
    sending = asyncio.create_task(service.send(request("order-456")))
    await asyncio.wait_for(transport.started.wait(), timeout=1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    transport.release.set()
    # Join the exact accepted delivery, not another call to send (which would
    # mask the old bug by cleaning it up on behalf of its cancelled caller).
    retained = service._inflight["order-456"]
    await asyncio.wait_for(asyncio.shield(retained), timeout=1)
    assert service._inflight == {}
    cached = await service.send(request("order-456"))
    assert cached.success and cached.deduplicated
    assert transport.calls == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_shutdown_drains_accepted_delivery_before_transport_close() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport})
    sending = asyncio.create_task(service.send(request()))
    await asyncio.wait_for(transport.started.wait(), timeout=1)
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    closed_early = transport.closed
    transport.release.set()
    result = await asyncio.wait_for(sending, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    assert closed_early == 0
    assert result.success
    assert transport.closed == 1


@pytest.mark.asyncio
async def test_failed_detached_delivery_releases_idempotency_key() -> None:
    class FailedTransport(LifecycleTransport):
        async def send(self, payload: NotificationPayload) -> TransportResult:
            await super().send(payload)
            if self.calls == 1:
                raise DeliveryError("temporarily unavailable", code="unavailable")
            return TransportResult(provider_status="accepted")

    transport = FailedTransport()
    service = NotificationService(transports={"test": transport})
    sending = asyncio.create_task(service.send(request("retry")))
    await asyncio.wait_for(transport.started.wait(), 1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    owned = service._inflight["retry"]
    transport.release.set()
    assert not await asyncio.wait_for(asyncio.shield(owned), 1)
    assert service.pending_deliveries == 0
    assert service._inflight == {}
    result = await service.send(request("retry"))
    assert result.success and not result.deduplicated
    assert transport.calls == 2
    await service.aclose()


@pytest.mark.asyncio
async def test_cancelling_one_duplicate_does_not_cancel_shared_delivery() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport}, max_pending_deliveries=1)
    first = asyncio.create_task(service.send(request("same-order")))
    await asyncio.wait_for(transport.started.wait(), 1)
    duplicate = asyncio.create_task(service.send(request("same-order")))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert service.pending_deliveries == 1
    busy = await service.send(request("another-order"))
    assert busy.error_code == "service_busy" and busy.retryable
    assert busy.attempts == 0
    transport.release.set()
    reused = await asyncio.wait_for(duplicate, 1)
    assert reused.success and reused.deduplicated
    assert service.pending_deliveries == 0
    assert transport.calls == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_admission_counts_queued_and_running_sends_and_releases_cancelled_slots() -> None:
    transport = LifecycleTransport()
    service = NotificationService(
        transports={"test": transport},
        concurrency_limit=1,
        max_pending_deliveries=2,
    )
    first = asyncio.create_task(service.send(request()))
    await asyncio.wait_for(transport.started.wait(), 1)
    queued = asyncio.create_task(service.send(request()))
    await asyncio.sleep(0)
    assert service.pending_deliveries == 2
    assert transport.calls == 1
    busy = await service.send(request())
    assert busy.error_code == "service_busy" and busy.attempts == 0
    assert service.get_delivery(busy.notification_id) == busy
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert service.pending_deliveries == 1
    replacement = asyncio.create_task(service.send(request()))
    transport.release.set()
    assert all(await asyncio.wait_for(asyncio.gather(first, replacement), 1))
    assert transport.calls == 2
    assert service.pending_deliveries == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_cancellation_before_delivery_coroutine_starts_releases_admission() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport}, max_pending_deliveries=1)
    sending = asyncio.create_task(service.send(request()))
    # Let send reserve a slot, then cancel before its newly scheduled task runs.
    await asyncio.sleep(0)
    assert service.pending_deliveries == 1
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert service.pending_deliveries == 0
    assert transport.calls == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_disabled_idempotency_does_not_shield_cancelled_caller() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport}, idempotency_cache_size=0)
    sending = asyncio.create_task(service.send(request("disabled")))
    await asyncio.wait_for(transport.started.wait(), 1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert transport.finished.is_set()
    assert service.pending_deliveries == 0
    assert service._inflight == {}
    await service.aclose()


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_running_and_queued_work_without_success() -> None:
    transport = LifecycleTransport()
    service = NotificationService(
        transports={"test": transport},
        concurrency_limit=1,
        shutdown_timeout_seconds=0.02,
    )
    sending = asyncio.create_task(service.send(request("running")))
    await asyncio.wait_for(transport.started.wait(), 1)
    queued = asyncio.create_task(service.send(request("queued")))
    await asyncio.sleep(0)
    with pytest.raises(NotificationError) as error:
        await asyncio.wait_for(service.aclose(), 1)
    assert error.value.code == "shutdown_timeout"
    for task in (sending, queued):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert transport.finished.is_set()
    assert transport.closed == 1
    assert transport.calls == 1
    assert service.pending_deliveries == 0
    assert service._inflight == {} and service._completed == {}
    assert not any(result.success for result in service.delivery_log)
    assert (await service.send(request())).error_code == "service_closed"
    # Repeated close reports the same terminal outcome without closing twice.
    with pytest.raises(NotificationError, match="grace period"):
        await service.aclose()
    assert transport.closed == 1


@pytest.mark.asyncio
async def test_cancelled_shutdown_waiter_does_not_interrupt_cleanup() -> None:
    transport = LifecycleTransport()
    service = NotificationService(transports={"test": transport})
    sending = asyncio.create_task(service.send(request("order")))
    await asyncio.wait_for(transport.started.wait(), 1)
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert (await service.send(request())).error_code == "service_closed"
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert transport.closed == 0
    transport.release.set()
    assert await asyncio.wait_for(sending, 1)
    await asyncio.wait_for(service.aclose(), 1)
    assert transport.closed == 1 and service.pending_deliveries == 0


@pytest.mark.asyncio
async def test_concurrent_shutdown_waiters_share_transport_cleanup() -> None:
    class SlowCloser(LifecycleTransport):
        async def aclose(self) -> None:
            self.closed += 1
            self.started.set()
            await self.release.wait()

    transport = SlowCloser()
    service = NotificationService(transports={"test": transport, "alias": transport})
    first = asyncio.create_task(service.aclose())
    await asyncio.wait_for(transport.started.wait(), 1)
    second = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert not second.done()
    transport.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert transport.closed == 1


@pytest.mark.asyncio
async def test_one_failed_closer_does_not_skip_others_or_leak_error_content() -> None:
    class BrokenCloser(LifecycleTransport):
        async def aclose(self) -> None:
            self.closed += 1
            raise RuntimeError("private connection details")

    broken = BrokenCloser()
    healthy = LifecycleTransport()
    service = NotificationService(transports={"broken": broken, "healthy": healthy})
    with pytest.raises(NotificationError) as error:
        await service.aclose()
    assert error.value.code == "transport_close_failed"
    assert "private" not in str(error.value)
    assert healthy.closed == broken.closed == 1


@pytest.mark.asyncio
async def test_transport_cleanup_phase_has_its_own_grace_period() -> None:
    class SlowCloser(LifecycleTransport):
        async def aclose(self) -> None:
            self.closed += 1
            try:
                await self.release.wait()
            finally:
                self.finished.set()

    slow = SlowCloser()
    healthy = LifecycleTransport()
    service = NotificationService(
        transports={"slow": slow, "healthy": healthy},
        shutdown_timeout_seconds=0.02,
    )
    with pytest.raises(NotificationError) as error:
        await asyncio.wait_for(service.aclose(), 1)
    assert error.value.code == "shutdown_timeout"
    assert slow.finished.is_set()
    assert slow.closed == healthy.closed == 1


@pytest.mark.parametrize("maximum", [0, -1, 100_001, 1.5, True, "10"])
def test_pending_limit_rejects_invalid_configuration(maximum: object) -> None:
    with pytest.raises(NotificationValidationError):
        NotificationService(config={"max_pending_deliveries": maximum})


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf"), True, "30"])
def test_shutdown_grace_rejects_invalid_configuration(timeout: object) -> None:
    with pytest.raises(NotificationValidationError):
        NotificationService(config={"shutdown_timeout_seconds": timeout})


@pytest.mark.asyncio
async def test_valid_lifecycle_config_mapping() -> None:
    service = NotificationService(
        config={"max_pending_deliveries": 1, "shutdown_timeout_seconds": 1}
    )
    assert service.pending_deliveries == 0
    await service.aclose()
    with pytest.raises(NotificationValidationError) as error:
        service.register_transport("late", LifecycleTransport())
    assert error.value.code == "service_closed"


@pytest.mark.asyncio
async def test_eager_task_factory_cannot_bypass_admission_via_reentrant_transport() -> None:
    factory = getattr(asyncio, "eager_task_factory", None)
    if factory is None:
        pytest.skip("eager task factory requires Python 3.12+")
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    nested_codes: list[str | None] = []

    class ReentrantTransport:
        calls = 0

        async def send(self, _payload: NotificationPayload) -> TransportResult:
            self.calls += 1
            # Keep the regression itself bounded if admission ever regresses.
            if self.calls < 3:
                nested_codes.append((await service.send(request())).error_code)
            return TransportResult(provider_status="accepted")

    transport = ReentrantTransport()
    service = NotificationService(transports={"test": transport}, max_pending_deliveries=1)
    loop.set_task_factory(factory)
    try:
        assert (await service.send(request())).success
        assert nested_codes == ["service_busy"]
        assert transport.calls == 1
        assert service.pending_deliveries == 0
        await service.aclose()
    finally:
        loop.set_task_factory(previous_factory)
