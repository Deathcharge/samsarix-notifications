# SPDX-License-Identifier: MPL-2.0
"""Small, local-first notification delivery primitives for Python applications."""

from .alert_system import Alert, AlertSeverity, AlertSystem
from .email_service import EmailAttachment, EmailService, EmailTemplate, SMTPConfig
from .errors import (
    ConfigurationError,
    DeliveryError,
    NotificationError,
    NotificationValidationError,
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
    "NotificationChannel",
    "NotificationError",
    "NotificationPayload",
    "NotificationService",
    "NotificationTransport",
    "NotificationValidationError",
    "RetryPolicy",
    "SMTPConfig",
    "TransportResult",
    "WebhookPolicy",
    "WebhookRoute",
    "WebhookRouter",
]
