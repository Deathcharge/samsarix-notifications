# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json

import pytest

from samsarix_notifications import NotificationValidationError
from samsarix_notifications._json import encode_json


def test_native_json_round_trip_and_exact_size() -> None:
    value = {"z": [None, True, 42, 1.5], "a": ("日本語", "\u0000")}
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert encode_json(value, max_bytes=len(expected.encode()), prefix="test", sort_keys=True) == (
        expected
    )
    with pytest.raises(NotificationValidationError) as error:
        encode_json(value, max_bytes=len(expected.encode()) - 1, prefix="test")
    assert error.value.code == "test_payload_too_large"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ({"value": "x" * 1025}, "too_large"),
        ({"value": "日" * 500}, "too_large"),
        ({"value": "\u0000" * 300}, "too_large"),
        ({"a": "x" * 600, "b": "x" * 600}, "too_large"),
        ({"value": "\ud800"}, "not_json"),
        ({1: "ambiguous key"}, "not_json"),
        ({"value": float("nan")}, "not_json"),
        ({"value": float("inf")}, "not_json"),
        ({"value": object()}, "not_json"),
        ({"value": 1 << 5000}, "too_complex"),
        ([None] * 10_000, "too_complex"),
        ({str(i): None for i in range(5000)}, "too_complex"),
    ],
)
def test_resource_bounds(value: object, code: str) -> None:
    with pytest.raises(NotificationValidationError) as error:
        encode_json(value, max_bytes=1024, prefix="test")
    assert error.value.code == f"test_payload_{code}"


def test_deep_and_cyclic_data_are_rejected_before_serialization() -> None:
    nested: object = None
    for _ in range(2000):
        nested = [nested]
    cyclic: list[object] = []
    cyclic.append(cyclic)
    for value in (nested, cyclic):
        with pytest.raises(NotificationValidationError) as error:
            encode_json(value, max_bytes=1024, prefix="test")
        assert error.value.code == "test_payload_too_complex"


def test_shared_values_are_allowed_but_expanded_work_is_bounded() -> None:
    child = [1, 2]
    assert encode_json([child, child], max_bytes=1024, prefix="test") == "[[1,2],[1,2]]"
    value: object = None
    for _ in range(20):
        value = [value, value]
    with pytest.raises(NotificationValidationError) as error:
        encode_json(value, max_bytes=1024, prefix="test")
    assert error.value.code == "test_payload_too_complex"
