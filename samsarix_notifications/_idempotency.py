# SPDX-License-Identifier: MPL-2.0
"""Bounded, typed identity and caller-independent snapshots for direct sends."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from typing import Any, NoReturn

from .email_service import EmailAttachment
from .errors import NotificationValidationError
from .models import NotificationPayload

_MAX_BYTES = 64 * 1024 * 1024
_MAX_NODES = 10_000
_MAX_DEPTH = 32


def snapshot_request(payload: NotificationPayload) -> tuple[NotificationPayload, bytes]:
    """Copy supported values and hash delivery intent, excluding ID/timestamp.

    No pickle, repr, custom equality, or user-defined copy hook participates in
    identity. Only immutable scalar data survives in a completed cache entry.
    """

    digest = hashlib.sha256()
    nodes = size = 0

    def reject(suffix: str) -> NoReturn:
        raise NotificationValidationError(
            "Idempotent payload exceeds limits or contains unsupported values",
            code=f"idempotency_payload_{suffix}",
        )

    def feed(tag: bytes, data: bytes = b"") -> None:
        nonlocal size
        size += len(data)
        if size > _MAX_BYTES:
            reject("too_large")
        # Length-framed, type-tagged fields cannot collide by concatenation.
        digest.update(tag)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    def visit(value: Any, depth: int = 0) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            reject("too_complex")
        kind = type(value)
        if value is None:
            feed(b"n")
        elif kind is bool:
            feed(b"b", b"1" if value else b"0")
        elif kind is int:
            if value.bit_length() > 4096:
                reject("too_complex")
            feed(b"i", str(value).encode("ascii"))
        elif kind is float:
            if not math.isfinite(value):
                reject("unsupported")
            feed(b"f", value.hex().encode("ascii"))
        elif kind is str:
            if len(value) > _MAX_BYTES - size:
                reject("too_large")
            try:
                encoded = value.encode("utf-8")
            except UnicodeError:
                reject("unsupported")
            feed(b"s", encoded)
        elif kind is bytes:
            feed(b"y", value)
        elif kind is dict:
            # Bound before sorting/copying or descending into a container.
            if nodes + 2 * len(value) > _MAX_NODES:
                reject("too_complex")
            if any(type(key) is not str for key in value):
                reject("unsupported")
            # Validate key text lengths before sorting potentially huge keys.
            if sum(len(key) for key in value) > _MAX_BYTES - size:
                reject("too_large")
            feed(b"d", len(value).to_bytes(8, "big"))
            copied = {}
            for key in sorted(value):
                visit(key, depth + 1)
                copied[key] = visit(value[key], depth + 1)
            return {key: copied[key] for key in value}
        elif kind is list or kind is tuple:
            if nodes + len(value) > _MAX_NODES:
                reject("too_complex")
            feed(b"l" if kind is list else b"t", len(value).to_bytes(8, "big"))
            items = [visit(child, depth + 1) for child in value]
            return items if kind is list else tuple(items)
        elif kind is EmailAttachment:
            feed(b"a")
            return EmailAttachment(
                visit(value.filename, depth + 1),
                visit(value.content, depth + 1),
                visit(value.content_type, depth + 1),
            )
        else:
            reject("unsupported")
        return value

    # Validate and normalize scalars again; the public payload is mutable.
    # Do not copy an unbounded caller metadata mapping before checking limits.
    if type(payload.metadata) is not dict:
        reject("unsupported")
    snapshot = replace(payload, metadata={})
    semantic = visit(
        {
            "channel": snapshot.channel,
            "recipient": snapshot.recipient,
            "subject": snapshot.subject,
            "body": snapshot.body,
            "priority": snapshot.priority,
            "retry_count": snapshot.retry_count,
            "max_retries": snapshot.max_retries,
            "metadata": payload.metadata,
        }
    )
    snapshot.metadata = semantic["metadata"]
    return snapshot, digest.digest()
