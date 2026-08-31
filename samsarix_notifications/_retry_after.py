# SPDX-License-Identifier: MPL-2.0
"""Bounded Retry-After parsing for HTTP response headers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ._validation import MAX_RETRY_AFTER_SECONDS
from .errors import DeliveryError

_RFC850_DATE = re.compile(
    r"[A-Za-z]+, [0-9]{2}-[A-Za-z]{3}-([0-9]{2}) [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Return a minimum delay; malformed values fall back to local backoff.

    Excessive values stop automatic retry instead of being clamped to an
    earlier time. Bound parsing before integer conversion or date processing.
    """
    if value is None:
        return None
    if len(value) > 128:
        raise DeliveryError(
            "Webhook Retry-After header exceeds the supported length",
            code="webhook_retry_after_unsupported",
        )
    value = value.strip(" \t")
    if value.isascii() and value.isdecimal():
        significant = value.lstrip("0") or "0"
        if len(significant) > 5:
            seconds = float(MAX_RETRY_AFTER_SECONDS + 1)
        else:
            seconds = float(int(significant))
    else:
        try:
            scheduled = parsedate_to_datetime(value)
            obsolete = _RFC850_DATE.fullmatch(value)
            if obsolete is not None:
                # HTTP's rolling 50-year window differs from email's fixed
                # 1969/2068 cutoff. Compare components to handle leap years.
                year = ((now.year + 50) // 100) * 100 + int(obsolete[1])
                candidate = (
                    year,
                    scheduled.month,
                    scheduled.day,
                    scheduled.hour,
                    scheduled.minute,
                    scheduled.second,
                )
                ceiling = (now.year + 50, now.month, now.day, now.hour, now.minute, now.second)
                if candidate > ceiling:
                    year -= 100
                scheduled = scheduled.replace(year=year)
            # Obsolete asctime HTTP dates have no zone but mean GMT.
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            seconds = max(0.0, (scheduled - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds > MAX_RETRY_AFTER_SECONDS:
        raise DeliveryError(
            "Webhook Retry-After exceeds the supported 86400-second retry window",
            code="webhook_retry_after_unsupported",
        )
    return seconds
