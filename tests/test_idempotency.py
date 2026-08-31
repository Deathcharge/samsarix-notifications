# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from samsarix_notifications import (
    DeliveryError,
    EmailAttachment,
    NotificationError,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    TransportResult,
)
from samsarix_notifications._idempotency import snapshot_request


class RecordingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[NotificationPayload] = []

    async def send(self, payload: NotificationPayload) -> TransportResult:
        self.started.set()
        await self.release.wait()
        self.requests.append(payload)
        return TransportResult(provider_status="accepted")


def request() -> NotificationPayload:
    return NotificationPayload(
        "test", "customer-456", "Order confirmed", "Order 456", idempotency_key="order-456"
    )


@pytest.mark.asyncio
async def test_cached_key_cannot_report_success_for_a_different_recipient() -> None:
    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(transports={"test": transport}) as service:
        original = request()
        assert await service.send(original)
        conflict = await service.send(replace(original, recipient="customer-789"))
        assert conflict.error_code == "idempotency_conflict"
        assert conflict.recipient == "customer-789"
        assert conflict.attempts == 0 and conflict.retryable is False
        assert not conflict.deduplicated and not conflict.success
        assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_accepted_payload_is_independent_of_later_caller_mutation() -> None:
    transport = RecordingTransport()
    async with NotificationService(transports={"test": transport}) as service:
        original = request()
        nested = {"items": ["first"]}
        original.metadata = nested
        sending = asyncio.create_task(service.send(original))
        await asyncio.wait_for(transport.started.wait(), 1)
        original.recipient = "changed-customer"
        nested["items"].append("changed-item")
        transport.release.set()
        result = await asyncio.wait_for(sending, 1)
        assert result.recipient == "customer-456"
        assert transport.requests[0].metadata == {"items": ["first"]}


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"channel": "other"},
        {"recipient": "other"},
        {"subject": "other"},
        {"body": "other"},
        {"metadata": {"event": "other"}},
        {"priority": "urgent"},
        {"retry_count": 1},
        {"max_retries": 2},
    ],
)
async def test_conflicting_intent_is_rejected_without_waiting_or_replacing_original(
    inflight: bool, changes: dict[str, Any]
) -> None:
    transport = RecordingTransport()
    service = NotificationService(transports={"test": transport}, max_pending_deliveries=1)
    original = request()
    sending = asyncio.create_task(service.send(original))
    await asyncio.wait_for(transport.started.wait(), 1)
    if not inflight:
        transport.release.set()
        await sending
    changed = replace(original, notification_id="conflicting-call", **changes)
    try:
        result = await asyncio.wait_for(service.send(changed), 1)
        assert result.error_code == "idempotency_conflict"
        assert not result.success and not result.deduplicated
        assert result.attempts == 0 and result.retryable is False
        assert result.notification_id == "conflicting-call"
        assert service.get_delivery("conflicting-call") == result
        assert result.error_message is not None
        assert original.body not in result.error_message
    finally:
        transport.release.set()
        assert await asyncio.wait_for(sending, 1)
        cached = await service.send(original)
        assert cached.success and cached.deduplicated
        assert len(transport.requests) == 1
        await service.aclose()
        assert service._inflight_fingerprints == {}


@pytest.mark.asyncio
async def test_regenerated_ids_timestamps_and_mapping_order_still_deduplicate() -> None:
    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(transports={"test": transport}) as service:
        original = request()
        original.metadata = {"z": [None, True, 7, 1.25], "a": {"y": "日", "x": b"data"}}
        result = await service.send(original)
        repeated = replace(
            original,
            notification_id="new-id",
            created_at=original.created_at + timedelta(days=1),
            metadata={"a": {"x": b"data", "y": "日"}, "z": [None, True, 7, 1.25]},
        )
        cached = await service.send(repeated)
        assert cached.deduplicated and cached.notification_id == result.notification_id
        assert len(transport.requests) == 1
        assert list(transport.requests[0].metadata) == ["z", "a"]


@pytest.mark.parametrize(
    ("first", "second"),
    [(True, 1), (1, 1.0), ("1", b"1"), ([1], (1,)), (["ab", "c"], ["a", "bc"])],
)
def test_identity_is_type_tagged_and_length_framed(first: object, second: object) -> None:
    assert (
        snapshot_request(replace(request(), metadata={"value": first}))[1]
        != snapshot_request(replace(request(), metadata={"value": second}))[1]
    )


@pytest.mark.parametrize("as_mapping", [False, True])
@pytest.mark.asyncio
async def test_attachment_content_changes_conflict_and_nested_containers_are_copied(
    as_mapping: bool,
) -> None:
    def attachment(content: bytes) -> object:
        if as_mapping:
            return {"filename": "report.txt", "content": content, "content_type": "text/plain"}
        return EmailAttachment("report.txt", content, "text/plain")

    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(transports={"test": transport}) as service:
        original = replace(request(), metadata={"attachments": [attachment(b"first")]})
        assert await service.send(original)
        assert (await service.send(original)).deduplicated
        conflict = await service.send(
            replace(original, metadata={"attachments": [attachment(b"different")]})
        )
        assert conflict.error_code == "idempotency_conflict"
        assert len(transport.requests) == 1
        assert transport.requests[0].metadata == original.metadata
        assert transport.requests[0].metadata is not original.metadata
        assert transport.requests[0].metadata["attachments"] is not original.metadata["attachments"]


@pytest.mark.parametrize(
    "value",
    [object(), {1: "bad-key"}, float("nan"), float("inf"), "\ud800", bytearray(b"mutable")],
)
@pytest.mark.asyncio
async def test_unsupported_identity_is_an_explicit_zero_attempt_failure(value: object) -> None:
    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(transports={"test": transport}) as service:
        result = await service.send(replace(request(), metadata={"private-value": value}))
        assert result.error_code == "idempotency_payload_unsupported"
        assert result.attempts == 0 and result.retryable is False
        assert not transport.requests and service.pending_deliveries == 0
        assert service._inflight_fingerprints == {}
        assert result.error_message is not None and "private-value" not in result.error_message
        # Validation did not reserve or poison the key.
        assert await service.send(request())


def test_custom_repr_equality_and_copy_hooks_are_not_called() -> None:
    class NoTypeEquality(type):
        def __eq__(cls, other: object) -> bool:
            raise AssertionError("must not compare caller types")

    class Unsupported(metaclass=NoTypeEquality):
        def __repr__(self) -> str:
            raise AssertionError("must not format caller objects")

        def __eq__(self, other: object) -> bool:
            raise AssertionError("must not compare caller objects")

        def __deepcopy__(self, memo: object) -> object:
            raise AssertionError("must not copy caller objects")

    with pytest.raises(NotificationValidationError) as error:
        snapshot_request(replace(request(), metadata={"value": Unsupported()}))
    assert error.value.code == "idempotency_payload_unsupported"


@pytest.mark.parametrize("value", [[None] * 10_000, {str(i): None for i in range(5000)}, 1 << 5000])
def test_identity_complexity_limits(value: object) -> None:
    with pytest.raises(NotificationValidationError) as error:
        snapshot_request(replace(request(), metadata={"value": value}))
    assert error.value.code == "idempotency_payload_too_complex"


def test_cyclic_deep_and_shared_expanding_values_are_bounded() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    nested: object = None
    shared: object = None
    for _ in range(40):
        nested = [nested]
    for _ in range(20):
        shared = [shared, shared]
    for value in (cyclic, nested, shared):
        with pytest.raises(NotificationValidationError) as error:
            snapshot_request(replace(request(), metadata={"value": value}))
        assert error.value.code == "idempotency_payload_too_complex"


@pytest.mark.parametrize("value", ["x" * 2048, b"x" * 2048, "日" * 500, {"x" * 2048: None}])
def test_identity_byte_budget_is_enforced_before_retention(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setattr("samsarix_notifications._idempotency._MAX_BYTES", 1024)
    with pytest.raises(NotificationValidationError) as error:
        snapshot_request(replace(request(), metadata={"value": value}))
    assert error.value.code == "idempotency_payload_too_large"


@pytest.mark.parametrize("cache_size", [0, 1])
@pytest.mark.asyncio
async def test_non_idempotent_custom_metadata_remains_supported(cache_size: int) -> None:
    marker = object()
    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(
        transports={"test": transport}, idempotency_cache_size=cache_size
    ) as service:
        payload = replace(
            request(),
            idempotency_key="disabled" if not cache_size else None,
            metadata={"m": marker},
        )
        assert await service.send(payload)
        assert transport.requests[0].metadata["m"] is marker
        assert service._inflight_fingerprints == {}


@pytest.mark.asyncio
async def test_failed_delivery_releases_fingerprint_and_allows_changed_intent() -> None:
    class FailsOnce:
        calls = 0

        async def send(self, payload: NotificationPayload) -> TransportResult:
            self.calls += 1
            if self.calls == 1:
                raise DeliveryError("failed", code="failed")
            return TransportResult()

    transport = FailsOnce()
    async with NotificationService(transports={"test": transport}) as service:
        assert not await service.send(request())
        assert service._inflight_fingerprints == {} and service._completed == {}
        assert await service.send(replace(request(), body="changed"))
        assert transport.calls == 2


@pytest.mark.asyncio
async def test_conflict_does_not_refresh_or_replace_lru_entry() -> None:
    transport = RecordingTransport()
    transport.release.set()
    async with NotificationService(
        transports={"test": transport}, idempotency_cache_size=2
    ) as service:
        assert await service.send(request())
        assert await service.send(replace(request(), idempotency_key="second"))
        assert (await service.send(replace(request(), body="changed"))).error_code == (
            "idempotency_conflict"
        )
        assert await service.send(replace(request(), idempotency_key="third"))
        # The original key was least-recently used and has now been evicted.
        assert "order-456" not in service._completed
        assert await service.send(replace(request(), body="changed"))
        assert len(transport.requests) == 4
        assert len(service._completed) == 2
        assert all(len(entry[0]) == 32 for entry in service._completed.values())


@pytest.mark.asyncio
async def test_cancelled_owner_keeps_identity_until_accepted_work_completes() -> None:
    transport = RecordingTransport()
    async with NotificationService(transports={"test": transport}) as service:
        original = request()
        sending = asyncio.create_task(service.send(original))
        await asyncio.wait_for(transport.started.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        try:
            conflict = await asyncio.wait_for(service.send(replace(original, body="changed")), 1)
            assert conflict.error_code == "idempotency_conflict"
            assert service.pending_deliveries == 1
        finally:
            transport.release.set()
        duplicate = await asyncio.wait_for(service.send(original), 1)
        assert duplicate.success and duplicate.deduplicated
        assert service._inflight_fingerprints == {}
        assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_shutdown_cancellation_releases_pending_fingerprints() -> None:
    transport = RecordingTransport()
    service = NotificationService(transports={"test": transport}, shutdown_timeout_seconds=0.01)
    sending = asyncio.create_task(service.send(request()))
    await asyncio.wait_for(transport.started.wait(), 1)
    with pytest.raises(NotificationError, match="grace period"):
        await service.aclose()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert service._inflight_fingerprints == {} and service._completed == {}


def test_mutating_metadata_to_a_non_mapping_does_not_create_an_invalid_snapshot() -> None:
    payload = request()
    payload.metadata = []  # type: ignore[assignment]
    with pytest.raises(NotificationValidationError) as error:
        snapshot_request(payload)
    assert error.value.code == "idempotency_payload_unsupported"
