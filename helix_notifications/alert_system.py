"""Bounded process-local alert state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import RLock
from typing import Any

from .errors import NotificationValidationError
from .models import utc_now


class AlertSeverity(str, Enum):
    """Alert severity levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass(slots=True)
class Alert:
    """A process-local alert record."""

    id: str
    title: str
    description: str
    severity: AlertSeverity | str
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("id", "title", "description", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise NotificationValidationError(f"alert {name} must be a non-empty string")
            if len(value) > 10_000:
                raise NotificationValidationError(f"alert {name} is too long")
            setattr(self, name, value.strip())
        if not isinstance(self.severity, AlertSeverity):
            try:
                self.severity = AlertSeverity(self.severity)
            except ValueError as exc:
                raise NotificationValidationError("alert severity is invalid") from exc
        if not isinstance(self.metadata, Mapping):
            raise NotificationValidationError("alert metadata must be a mapping")
        self.metadata = dict(self.metadata)


class AlertSystem:
    """Store a bounded set of active alerts in the current process."""

    def __init__(self, *, max_alerts: int = 10_000) -> None:
        if not 1 <= max_alerts <= 1_000_000:
            raise NotificationValidationError("max_alerts must be between 1 and 1000000")
        self.alerts: dict[str, Alert] = {}
        self.escalation_policies: dict[str, list[str]] = {}
        self._max_alerts = max_alerts
        self._lock = RLock()

    async def create_alert(self, alert: Alert) -> str:
        """Create a unique alert, failing explicitly at the capacity bound."""

        if not isinstance(alert, Alert):
            raise NotificationValidationError("alert must be an Alert")
        with self._lock:
            if alert.id in self.alerts:
                raise NotificationValidationError(
                    f"alert already exists: {alert.id}", code="duplicate_alert"
                )
            if len(self.alerts) >= self._max_alerts:
                raise NotificationValidationError(
                    "active alert capacity has been reached", code="alert_capacity_reached"
                )
            self.alerts[alert.id] = alert
        return alert.id

    async def resolve_alert(self, alert_id: str) -> bool:
        """Resolve an alert and report whether it existed."""

        with self._lock:
            return self.alerts.pop(alert_id, None) is not None

    def get_active_alerts(self, severity: AlertSeverity | None = None) -> list[Alert]:
        """Return a snapshot, optionally filtered by severity."""

        with self._lock:
            alerts = list(self.alerts.values())
        if severity is not None:
            alerts = [alert for alert in alerts if alert.severity is severity]
        return alerts
