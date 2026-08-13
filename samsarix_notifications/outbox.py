# SPDX-License-Identifier: MPL-2.0
"""Crash-safe SQLite outbox and asynchronous delivery worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .errors import NotificationValidationError, OutboxConflictError, OutboxLeaseError
from .models import DeliveryResult, NotificationPayload, utc_now
from .notification_service import NotificationService

_TABLE = "samsarix_notification_outbox"
_MAX_ENCODED_PAYLOAD_BYTES = 2 * 1024 * 1024
_SCHEMA_STATEMENTS = (
    f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
    message_id TEXT PRIMARY KEY,
    idempotency_key TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'delivered', 'dead_letter')),
    enqueued_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    provider_id TEXT,
    provider_status TEXT
);""",
    f"""CREATE UNIQUE INDEX IF NOT EXISTS samsarix_outbox_idempotency_key
    ON {_TABLE}(idempotency_key)
    WHERE idempotency_key IS NOT NULL;""",
    f"""CREATE INDEX IF NOT EXISTS samsarix_outbox_ready
    ON {_TABLE}(status, available_at, enqueued_at);""",
)


class OutboxStatus(str, Enum):
    """Durable lifecycle state of an outbox message."""

    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A persisted notification and its current durable state."""

    message_id: str
    payload: NotificationPayload
    status: OutboxStatus
    enqueued_at: datetime
    available_at: datetime
    attempt_count: int
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    provider_id: str | None = None
    provider_status: str | None = None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Result of an idempotent outbox enqueue."""

    message: OutboxMessage
    created: bool


@dataclass(frozen=True, slots=True)
class OutboxRunResult:
    """Bounded summary returned by one worker pass."""

    claimed: int = 0
    delivered: int = 0
    rescheduled: int = 0
    dead_lettered: int = 0


@dataclass(frozen=True, slots=True)
class _ClaimResult:
    message: OutboxMessage | None
    quarantined: int = 0


class SQLiteOutbox:
    """Store notifications durably in a local SQLite database.

    Each operation uses a short-lived connection so separate processes can
    cooperate through SQLite locking. ``enqueue`` optionally accepts an
    existing connection, allowing the notification row and application data to
    be committed atomically when both use the same database.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        busy_timeout_seconds: float = 5.0,
        initialize: bool = True,
    ) -> None:
        path = str(database)
        if not path or path == ":memory:":
            raise NotificationValidationError(
                "outbox database must be a persistent SQLite path",
                code="invalid_outbox_database",
            )
        if not 0 < busy_timeout_seconds <= 300:
            raise NotificationValidationError("busy_timeout_seconds must be between 0 and 300")
        self.database = path
        self.busy_timeout_seconds = busy_timeout_seconds
        if initialize:
            self.initialize()

    def initialize(self, connection: sqlite3.Connection | None = None) -> None:
        """Create the outbox schema without modifying application tables."""

        own_connection = connection is None
        active = connection or self._connect()
        previous_row_factory = active.row_factory
        active.row_factory = sqlite3.Row
        try:
            if own_connection:
                active.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                active.execute(statement)
            columns = {
                str(row[1]) for row in active.execute(f"PRAGMA table_info({_TABLE})").fetchall()
            }
            if "lease_token" not in columns:
                active.execute(f"ALTER TABLE {_TABLE} ADD COLUMN lease_token TEXT")
            if own_connection:
                active.commit()
        except Exception:
            if own_connection:
                active.rollback()
            raise
        finally:
            if not own_connection:
                active.row_factory = previous_row_factory
            if own_connection:
                active.close()

    def enqueue(
        self,
        payload: NotificationPayload,
        *,
        available_at: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> EnqueueResult:
        """Persist a notification, deduplicating matching idempotency keys.

        When ``connection`` is supplied, transaction ownership stays with the
        caller. The connection must address a database where ``initialize`` has
        already created the outbox schema.
        """

        if not isinstance(payload, NotificationPayload):
            raise NotificationValidationError("payload must be a NotificationPayload")
        scheduled_for = _normalized_time(available_at or utc_now(), name="available_at")
        encoded, fingerprint = _encode_payload(payload)
        enqueued_at = utc_now()
        own_connection = connection is None
        active = connection or self._connect()
        previous_row_factory = active.row_factory
        active.row_factory = sqlite3.Row
        try:
            if own_connection:
                active.execute("BEGIN IMMEDIATE")
            existing = self._find_existing(active, payload)
            if existing is not None:
                self._verify_fingerprint(existing, fingerprint)
                if own_connection:
                    active.commit()
                return EnqueueResult(_row_to_message(existing), created=False)
            try:
                active.execute(
                    f"""
                    INSERT INTO {_TABLE} (
                        message_id, idempotency_key, payload_json, payload_sha256,
                        status, enqueued_at, available_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.notification_id,
                        payload.idempotency_key,
                        encoded,
                        fingerprint,
                        OutboxStatus.PENDING.value,
                        _encode_time(enqueued_at),
                        _encode_time(scheduled_for),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._find_existing(active, payload)
                if existing is None:
                    raise
                self._verify_fingerprint(existing, fingerprint)
                if own_connection:
                    active.commit()
                return EnqueueResult(_row_to_message(existing), created=False)
            row = active.execute(
                f"SELECT * FROM {_TABLE} WHERE message_id = ?",
                (payload.notification_id,),
            ).fetchone()
            assert row is not None
            if own_connection:
                active.commit()
            return EnqueueResult(_row_to_message(row), created=True)
        except Exception:
            if own_connection:
                active.rollback()
            raise
        finally:
            if not own_connection:
                active.row_factory = previous_row_factory
            if own_connection:
                active.close()

    async def aenqueue(
        self,
        payload: NotificationPayload,
        *,
        available_at: datetime | None = None,
    ) -> EnqueueResult:
        """Persist a notification without blocking the event loop."""

        return await asyncio.to_thread(self.enqueue, payload, available_at=available_at)

    def get(self, message_id: str) -> OutboxMessage | None:
        """Return one message by its stable notification ID."""

        _validate_identifier("message_id", message_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT * FROM {_TABLE} WHERE message_id = ?", (message_id,)
            ).fetchone()
        return _row_to_message(row) if row is not None else None

    def list_messages(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 100,
    ) -> tuple[OutboxMessage, ...]:
        """List messages in enqueue order with a hard result bound."""

        if not 1 <= limit <= 10_000:
            raise NotificationValidationError("limit must be between 1 and 10000")
        query = f"SELECT * FROM {_TABLE}"
        parameters: tuple[object, ...] = ()
        if status is not None:
            if not isinstance(status, OutboxStatus):
                raise NotificationValidationError("status must be an OutboxStatus")
            query += " WHERE status = ?"
            parameters = (status.value,)
        query += " ORDER BY enqueued_at, message_id LIMIT ?"
        parameters += (limit,)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_row_to_message(row) for row in rows)

    def counts(self) -> dict[OutboxStatus, int]:
        """Return message counts for every lifecycle state."""

        counts = {status: 0 for status in OutboxStatus}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM {_TABLE} GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[OutboxStatus(str(row["status"]))] = int(row["count"])
        return counts

    def requeue_dead_letter(
        self,
        message_id: str,
        *,
        available_at: datetime | None = None,
    ) -> OutboxMessage:
        """Return a dead-lettered message to the pending queue."""

        _validate_identifier("message_id", message_id)
        scheduled_for = _normalized_time(available_at or utc_now(), name="available_at")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE {_TABLE}
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, completed_at = NULL,
                    attempt_count = 0, last_error_code = NULL,
                    last_error_message = NULL, provider_id = NULL,
                    provider_status = NULL
                WHERE message_id = ? AND status = ?
                """,
                (
                    OutboxStatus.PENDING.value,
                    _encode_time(scheduled_for),
                    message_id,
                    OutboxStatus.DEAD_LETTER.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OutboxConflictError(
                    "message is not dead-lettered",
                    code="outbox_message_not_dead_lettered",
                )
            row = connection.execute(
                f"SELECT * FROM {_TABLE} WHERE message_id = ?", (message_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return _row_to_message(row)

    def purge_completed(self, *, before: datetime, limit: int = 1_000) -> int:
        """Delete old delivered messages in a bounded transaction."""

        cutoff = _normalized_time(before, name="before")
        if not 1 <= limit <= 10_000:
            raise NotificationValidationError("limit must be between 1 and 10000")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                DELETE FROM {_TABLE}
                WHERE message_id IN (
                    SELECT message_id FROM {_TABLE}
                    WHERE status = ? AND completed_at < ?
                    ORDER BY completed_at
                    LIMIT ?
                )
                """,
                (OutboxStatus.DELIVERED.value, _encode_time(cutoff), limit),
            )
            deleted = cursor.rowcount
            connection.commit()
        return deleted

    def _claim(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime,
        quarantine_limit: int,
    ) -> _ClaimResult:
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        lease_token = str(uuid4())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            quarantined = 0
            while True:
                row = connection.execute(
                    f"""
                    SELECT * FROM {_TABLE}
                    WHERE
                        (status = ? AND available_at <= ?)
                        OR
                        (status = ? AND lease_expires_at <= ?)
                    ORDER BY available_at, enqueued_at, message_id
                    LIMIT 1
                    """,
                    (
                        OutboxStatus.PENDING.value,
                        _encode_time(now),
                        OutboxStatus.PROCESSING.value,
                        _encode_time(now),
                    ),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return _ClaimResult(None, quarantined)
                try:
                    _row_to_message(row)
                except OutboxConflictError:
                    self._quarantine_corrupt_row(connection, row, now=now)
                    quarantined += 1
                    if quarantined >= quarantine_limit:
                        connection.commit()
                        return _ClaimResult(None, quarantined)
                    continue
                break
            message_id = str(row["message_id"])
            connection.execute(
                f"""
                UPDATE {_TABLE}
                SET status = ?, lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1
                WHERE message_id = ?
                """,
                (
                    OutboxStatus.PROCESSING.value,
                    worker_id,
                    lease_token,
                    _encode_time(lease_expires_at),
                    message_id,
                ),
            )
            claimed = connection.execute(
                f"SELECT * FROM {_TABLE} WHERE message_id = ?", (message_id,)
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return _ClaimResult(_row_to_message(claimed), quarantined)

    def _quarantine_structurally_corrupt(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM {_TABLE}
                WHERE status NOT IN (?, ?, ?, ?)
                   OR (
                       status IN (?, ?)
                       AND (
                           typeof(attempt_count) != 'integer'
                           OR attempt_count < 0
                           OR julianday(enqueued_at) IS NULL
                           OR julianday(available_at) IS NULL
                           OR (lease_expires_at IS NOT NULL AND julianday(lease_expires_at) IS NULL)
                           OR (completed_at IS NOT NULL AND julianday(completed_at) IS NULL)
                       )
                   )
                ORDER BY enqueued_at, message_id
                LIMIT ?
                """,
                (
                    *(status.value for status in OutboxStatus),
                    OutboxStatus.PENDING.value,
                    OutboxStatus.PROCESSING.value,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                self._quarantine_corrupt_row(connection, row, now=now)
            connection.commit()
        return len(rows)

    @staticmethod
    def _quarantine_corrupt_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> None:
        cursor = connection.execute(
            f"""
            UPDATE {_TABLE}
            SET status = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                completed_at = ?, last_error_code = ?, last_error_message = ?,
                provider_id = NULL, provider_status = NULL
            WHERE message_id = ?
            """,
            (
                OutboxStatus.DEAD_LETTER.value,
                _encode_time(now),
                "outbox_record_corrupt",
                "stored outbox record failed integrity validation",
                str(row["message_id"]),
            ),
        )
        if cursor.rowcount != 1:
            raise OutboxConflictError(
                "corrupt outbox record could not be quarantined",
                code="outbox_quarantine_failed",
            )

    def _mark_delivered(
        self,
        message: OutboxMessage,
        *,
        worker_id: str,
        result: DeliveryResult,
    ) -> None:
        self._finish_claim(
            message,
            worker_id=worker_id,
            status=OutboxStatus.DELIVERED,
            completed_at=result.completed_at,
            provider_id=result.provider_id,
            provider_status=result.provider_status,
        )

    def _reschedule(
        self,
        message: OutboxMessage,
        *,
        worker_id: str,
        available_at: datetime,
        result: DeliveryResult,
    ) -> None:
        self._finish_claim(
            message,
            worker_id=worker_id,
            status=OutboxStatus.PENDING,
            available_at=available_at,
            last_error_code=result.error_code,
            last_error_message=result.error_message,
        )

    def _mark_dead_letter(
        self,
        message: OutboxMessage,
        *,
        worker_id: str,
        result: DeliveryResult,
    ) -> None:
        self._finish_claim(
            message,
            worker_id=worker_id,
            status=OutboxStatus.DEAD_LETTER,
            completed_at=result.completed_at,
            last_error_code=result.error_code,
            last_error_message=result.error_message,
        )

    def _finish_claim(
        self,
        message: OutboxMessage,
        *,
        worker_id: str,
        status: OutboxStatus,
        available_at: datetime | None = None,
        completed_at: datetime | None = None,
        last_error_code: str | None = None,
        last_error_message: str | None = None,
        provider_id: str | None = None,
        provider_status: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE {_TABLE}
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, completed_at = ?,
                    last_error_code = ?, last_error_message = ?,
                    provider_id = ?, provider_status = ?
                WHERE message_id = ? AND status = ? AND lease_owner = ?
                  AND lease_token = ?
                """,
                (
                    status.value,
                    _encode_time(available_at or message.available_at),
                    _encode_time(completed_at) if completed_at is not None else None,
                    _bounded_optional(last_error_code, maximum=128),
                    _bounded_optional(last_error_message, maximum=2_048),
                    _bounded_optional(provider_id, maximum=128),
                    _bounded_optional(provider_status, maximum=128),
                    message.message_id,
                    OutboxStatus.PROCESSING.value,
                    worker_id,
                    message.lease_token,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OutboxLeaseError(
                    "outbox lease is no longer owned by this worker",
                    code="outbox_lease_lost",
                )
            connection.commit()

    def _find_existing(
        self,
        connection: sqlite3.Connection,
        payload: NotificationPayload,
    ) -> sqlite3.Row | None:
        if payload.idempotency_key is not None:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    f"SELECT * FROM {_TABLE} WHERE idempotency_key = ?",
                    (payload.idempotency_key,),
                ).fetchone(),
            )
            if row is not None:
                return row
        return cast(
            sqlite3.Row | None,
            connection.execute(
                f"SELECT * FROM {_TABLE} WHERE message_id = ?",
                (payload.notification_id,),
            ).fetchone(),
        )

    @staticmethod
    def _verify_fingerprint(row: sqlite3.Row, fingerprint: str) -> None:
        if str(row["payload_sha256"]) != fingerprint:
            raise OutboxConflictError(
                "idempotency key or notification ID was reused for a different payload",
                code="outbox_idempotency_conflict",
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1_000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class OutboxWorker:
    """Claim and deliver durable outbox messages with at-least-once semantics."""

    def __init__(
        self,
        outbox: SQLiteOutbox,
        service: NotificationService,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 300.0,
        max_delivery_attempts: int = 5,
        base_delay_seconds: float = 5.0,
        max_delay_seconds: float = 300.0,
    ) -> None:
        if not isinstance(outbox, SQLiteOutbox):
            raise NotificationValidationError("outbox must be a SQLiteOutbox")
        if not isinstance(service, NotificationService):
            raise NotificationValidationError("service must be a NotificationService")
        if not 1 <= lease_seconds <= 86_400:
            raise NotificationValidationError("lease_seconds must be between 1 and 86400")
        if not 1 <= max_delivery_attempts <= 100:
            raise NotificationValidationError("max_delivery_attempts must be between 1 and 100")
        if not 0 <= base_delay_seconds <= 3_600:
            raise NotificationValidationError("base_delay_seconds must be between 0 and 3600")
        if not base_delay_seconds <= max_delay_seconds <= 86_400:
            raise NotificationValidationError(
                "max_delay_seconds must be between base_delay_seconds and 86400"
            )
        self.outbox = outbox
        self.service = service
        self.worker_id = str(uuid4()) if worker_id is None else worker_id
        _validate_identifier("worker_id", self.worker_id)
        self.lease_seconds = lease_seconds
        self.max_delivery_attempts = max_delivery_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds

    async def run_once(self, *, limit: int = 100) -> OutboxRunResult:
        """Deliver up to ``limit`` currently available messages."""

        if not 1 <= limit <= 10_000:
            raise NotificationValidationError("limit must be between 1 and 10000")
        claimed = delivered = rescheduled = 0
        remaining = limit
        dead_lettered = await asyncio.to_thread(
            self.outbox._quarantine_structurally_corrupt,
            now=utc_now(),
            limit=remaining,
        )
        remaining -= dead_lettered
        while remaining > 0:
            now = utc_now()
            claim = await asyncio.to_thread(
                self.outbox._claim,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                now=now,
                quarantine_limit=remaining,
            )
            dead_lettered += claim.quarantined
            remaining -= claim.quarantined
            message = claim.message
            if message is None:
                if claim.quarantined and remaining:
                    continue
                break
            claimed += 1
            remaining -= 1
            result = await self.service._send_without_retries(message.payload)
            if result.success:
                await asyncio.to_thread(
                    self.outbox._mark_delivered,
                    message,
                    worker_id=self.worker_id,
                    result=result,
                )
                delivered += 1
            elif result.retryable and message.attempt_count < self.max_delivery_attempts:
                available_at = utc_now() + timedelta(
                    seconds=self._delay_before_attempt(message.attempt_count + 1)
                )
                await asyncio.to_thread(
                    self.outbox._reschedule,
                    message,
                    worker_id=self.worker_id,
                    available_at=available_at,
                    result=result,
                )
                rescheduled += 1
            else:
                await asyncio.to_thread(
                    self.outbox._mark_dead_letter,
                    message,
                    worker_id=self.worker_id,
                    result=result,
                )
                dead_lettered += 1
        return OutboxRunResult(claimed, delivered, rescheduled, dead_lettered)

    async def run_forever(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 100,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Poll until cancelled or ``stop_event`` is set."""

        if not 0.01 <= poll_interval_seconds <= 300:
            raise NotificationValidationError("poll_interval_seconds must be between 0.01 and 300")
        if not 1 <= batch_size <= 10_000:
            raise NotificationValidationError("batch_size must be between 1 and 10000")
        while stop_event is None or not stop_event.is_set():
            result = await self.run_once(limit=batch_size)
            if result.claimed:
                continue
            if stop_event is None:
                await asyncio.sleep(poll_interval_seconds)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def _delay_before_attempt(self, attempt_number: int) -> float:
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * float(2 ** max(0, attempt_number - 2)),
        )


def _encode_payload(payload: NotificationPayload) -> tuple[str, str]:
    semantic = {
        "channel": str(payload.channel),
        "recipient": payload.recipient,
        "subject": payload.subject,
        "body": payload.body,
        "metadata": dict(payload.metadata),
        "priority": payload.priority,
        "retry_count": payload.retry_count,
        "max_retries": payload.max_retries,
        "idempotency_key": payload.idempotency_key,
    }
    stored = {
        **semantic,
        "created_at": _encode_time(payload.created_at),
        "notification_id": payload.notification_id,
    }
    encoded = _json_dumps(stored)
    if len(encoded.encode("utf-8")) > _MAX_ENCODED_PAYLOAD_BYTES:
        raise NotificationValidationError(
            "encoded outbox payload exceeds 2 MiB",
            code="outbox_payload_too_large",
        )
    fingerprint = hashlib.sha256(_json_dumps(semantic).encode("utf-8")).hexdigest()
    return encoded, fingerprint


def _decode_payload(encoded: str) -> NotificationPayload:
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise OutboxConflictError(
            "stored outbox payload is invalid JSON", code="outbox_payload_corrupt"
        ) from exc
    if not isinstance(value, dict):
        raise OutboxConflictError(
            "stored outbox payload is not an object", code="outbox_payload_corrupt"
        )
    data = cast(dict[str, Any], value)
    try:
        created_at = datetime.fromisoformat(str(data["created_at"]))
        metadata = data["metadata"]
        if not isinstance(metadata, Mapping):
            raise TypeError
        return NotificationPayload(
            channel=data["channel"],
            recipient=data["recipient"],
            subject=data["subject"],
            body=data["body"],
            metadata=cast(Mapping[str, Any], metadata),
            priority=data["priority"],
            retry_count=data["retry_count"],
            max_retries=data["max_retries"],
            created_at=created_at,
            idempotency_key=data["idempotency_key"],
            notification_id=data["notification_id"],
        )
    except (KeyError, TypeError, ValueError, NotificationValidationError) as exc:
        raise OutboxConflictError(
            "stored outbox payload is invalid", code="outbox_payload_corrupt"
        ) from exc


def _json_dumps(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise NotificationValidationError(
            "outbox payload metadata must be JSON-serializable",
            code="outbox_payload_not_json",
        ) from exc


def _row_to_message(row: sqlite3.Row) -> OutboxMessage:
    try:
        message_id = str(row["message_id"])
        payload = _decode_payload(str(row["payload_json"]))
        if payload.notification_id != message_id:
            raise OutboxConflictError(
                "stored outbox payload identity does not match its row",
                code="outbox_payload_corrupt",
            )
        _, fingerprint = _encode_payload(payload)
        if fingerprint != str(row["payload_sha256"]):
            raise OutboxConflictError(
                "stored outbox payload fingerprint does not match",
                code="outbox_payload_corrupt",
            )
        return OutboxMessage(
            message_id=message_id,
            payload=payload,
            status=OutboxStatus(str(row["status"])),
            enqueued_at=_decode_time(row["enqueued_at"]),
            available_at=_decode_time(row["available_at"]),
            attempt_count=int(row["attempt_count"]),
            lease_owner=_optional_string(row["lease_owner"]),
            lease_token=_optional_string(row["lease_token"]),
            lease_expires_at=_decode_optional_time(row["lease_expires_at"]),
            completed_at=_decode_optional_time(row["completed_at"]),
            last_error_code=_optional_string(row["last_error_code"]),
            last_error_message=_optional_string(row["last_error_message"]),
            provider_id=_optional_string(row["provider_id"]),
            provider_status=_optional_string(row["provider_status"]),
        )
    except OutboxConflictError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, NotificationValidationError) as exc:
        raise OutboxConflictError(
            "stored outbox record is invalid", code="outbox_record_corrupt"
        ) from exc


def _normalized_time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise NotificationValidationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _encode_time(value: datetime) -> str:
    return _normalized_time(value, name="datetime").isoformat(timespec="microseconds")


def _decode_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise OutboxConflictError(
            "stored outbox timestamp is invalid", code="outbox_record_corrupt"
        ) from exc
    return _normalized_time(parsed, name="stored datetime")


def _decode_optional_time(value: object) -> datetime | None:
    return None if value is None else _decode_time(value)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _bounded_optional(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    return value[:maximum]


def _validate_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise NotificationValidationError(f"{name} must contain 1 to 128 characters")
