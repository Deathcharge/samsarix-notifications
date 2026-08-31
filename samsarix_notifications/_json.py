# SPDX-License-Identifier: MPL-2.0
"""Bound JSON traversal and allocation before encoding caller-controlled data."""

from __future__ import annotations

import json
import math

from .errors import NotificationValidationError


def encode_json(value: object, *, max_bytes: int, prefix: str, sort_keys: bool = False) -> str:
    """Encode native JSON values within depth, node, text, and output budgets."""

    def reject(suffix: str) -> None:
        raise NotificationValidationError(
            f"{prefix} payload exceeds JSON limits or contains unsupported values",
            code=f"{prefix}_payload_{suffix}",
        )

    # Inspect before calling the encoder: iterencode alone can still allocate
    # one enormous escaped string, sort a huge dictionary, or recurse too far.
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = text_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 10_000 or depth > 32:
            reject("too_complex")
        if isinstance(item, str):
            if len(item) > max_bytes:
                reject("too_large")
            try:
                text_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                reject("not_json")
            if text_bytes > max_bytes:
                reject("too_large")
        elif isinstance(item, dict):
            if nodes + len(pending) + 2 * len(item) > 10_000:
                reject("too_complex")
            for key, child in item.items():
                if not isinstance(key, str):
                    reject("not_json")
                pending.extend(((key, depth + 1), (child, depth + 1)))
        elif isinstance(item, (list, tuple)):
            if nodes + len(pending) + len(item) > 10_000:
                reject("too_complex")
            pending.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, bool):
            continue
        elif isinstance(item, int):
            if item.bit_length() > 4096:
                reject("too_complex")
        elif isinstance(item, float):
            if not math.isfinite(item):
                reject("not_json")
        else:
            reject("not_json")

    chunks: list[str] = []
    size = 0
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, sort_keys=sort_keys, separators=(",", ":")
        )
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > max_bytes:
                reject("too_large")
            chunks.append(chunk)
    except NotificationValidationError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise NotificationValidationError(
            f"{prefix} payload must contain JSON-serializable values",
            code=f"{prefix}_payload_not_json",
        ) from exc
    return "".join(chunks)
