# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import samsarix_notifications


def test_public_package_shape() -> None:
    assert samsarix_notifications.__version__ == "0.1.0"
    expected = {
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
    }
    assert set(samsarix_notifications.__all__) == expected
