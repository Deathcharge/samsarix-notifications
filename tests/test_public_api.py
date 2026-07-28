from __future__ import annotations

import helix_notifications


def test_public_package_shape() -> None:
    assert helix_notifications.__version__ == "0.1.0"
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
    }
    assert set(helix_notifications.__all__) == expected
