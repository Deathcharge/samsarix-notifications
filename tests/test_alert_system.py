from __future__ import annotations

import pytest

from helix_notifications import (
    Alert,
    AlertSeverity,
    AlertSystem,
    NotificationValidationError,
)


def alert(identifier: str = "alert-1", severity: AlertSeverity = AlertSeverity.HIGH) -> Alert:
    return Alert(identifier, "Database latency", "p99 exceeded 1s", severity, "api")


@pytest.mark.asyncio
async def test_alert_lifecycle_and_filtering() -> None:
    alerts = AlertSystem()
    first = alert()
    second = alert("alert-2", AlertSeverity.INFO)

    assert await alerts.create_alert(first) == "alert-1"
    await alerts.create_alert(second)
    assert alerts.get_active_alerts(AlertSeverity.HIGH) == [first]
    assert alerts.get_active_alerts() == [first, second]
    assert await alerts.resolve_alert("alert-1")
    assert not await alerts.resolve_alert("missing")


@pytest.mark.asyncio
async def test_alert_duplicates_and_capacity_fail_explicitly() -> None:
    alerts = AlertSystem(max_alerts=1)
    await alerts.create_alert(alert())
    with pytest.raises(NotificationValidationError) as duplicate:
        await alerts.create_alert(alert())
    assert duplicate.value.code == "duplicate_alert"
    with pytest.raises(NotificationValidationError) as full:
        await alerts.create_alert(alert("alert-2"))
    assert full.value.code == "alert_capacity_reached"


def test_alert_validation() -> None:
    with pytest.raises(NotificationValidationError):
        Alert("", "title", "description", AlertSeverity.HIGH, "source")
    with pytest.raises(NotificationValidationError):
        Alert("id", "title", "description", "unknown", "source")  # type: ignore[arg-type]
    with pytest.raises(NotificationValidationError):
        AlertSystem(max_alerts=0)
    with pytest.raises(NotificationValidationError):
        Alert("id", "x" * 10_001, "description", AlertSeverity.HIGH, "source")
    with pytest.raises(NotificationValidationError):
        Alert("id", "title", "description", AlertSeverity.HIGH, "source", metadata="bad")


@pytest.mark.asyncio
async def test_alert_system_rejects_wrong_object_type() -> None:
    with pytest.raises(NotificationValidationError):
        await AlertSystem().create_alert(object())  # type: ignore[arg-type]
