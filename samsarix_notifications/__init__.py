# SPDX-License-Identifier: MPL-2.0
"""Small, local-first notification delivery primitives for Python applications."""

from .alert_system import Alert, AlertSeverity, AlertSystem
from .email_service import EmailAttachment, EmailService, EmailTemplate, SMTPConfig
from .errors import (
    ConfigurationError,
    DeliveryError,
    NotificationError,
    NotificationValidationError,
    OutboxConflictError,
    OutboxLeaseError,
)
from .models import (
    DeliveryResult,
    DeliveryStatus,
    NotificationChannel,
    NotificationPayload,
    RetryPolicy,
    TransportResult,
)
from .notification_service import NotificationService, NotificationTransport
from .outbox import (
    EnqueueResult,
    OutboxMessage,
    OutboxRunResult,
    OutboxStatus,
    OutboxWorker,
    SQLiteOutbox,
)
from .webhook_router import WebhookPolicy, WebhookRoute, WebhookRouter

__version__ = "0.1.0"
__author__ = "Samsarix LLC"

__all__ = [
    "Alert",
    "AlertSeverity",
    "AlertSystem",
    "ConfigurationError",
    "DeliveryError",
    "DeliveryResult",
    "DeliveryStatus",
    "EmailAttachment",
    "EmailService",
    "EmailTemplate",
    "EnqueueResult",
    "NotificationChannel",
    "NotificationError",
    "NotificationPayload",
    "NotificationService",
    "NotificationTransport",
    "NotificationValidationError",
    "OutboxConflictError",
    "OutboxLeaseError",
    "OutboxMessage",
    "OutboxRunResult",
    "OutboxStatus",
    "OutboxWorker",
    "RetryPolicy",
    "SMTPConfig",
    "SQLiteOutbox",
    "TransportResult",
    "WebhookPolicy",
    "WebhookRoute",
    "WebhookRouter",
]
