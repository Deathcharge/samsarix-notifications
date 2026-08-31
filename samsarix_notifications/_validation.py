# SPDX-License-Identifier: MPL-2.0
"""Shared native numeric predicates for configuration and resource bounds."""

from __future__ import annotations


def is_bounded_int(value: object, minimum: int, maximum: int) -> bool:
    """Require a native integer, excluding booleans and implicit conversion."""

    return type(value) is int and minimum <= value <= maximum


def is_bounded_number(
    value: object, minimum: float, maximum: float, *, exclusive_minimum: bool = False
) -> bool:
    """Require a finite native int/float within finite, caller-supplied bounds."""

    if type(value) is not int and type(value) is not float:
        return False
    lower_ok = minimum < value if exclusive_minimum else minimum <= value
    return lower_ok and value <= maximum
