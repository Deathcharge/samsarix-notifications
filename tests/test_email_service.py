# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import smtplib
from dataclasses import replace
from email.message import EmailMessage
from typing import Any

import pytest

from samsarix_notifications import (
    ConfigurationError,
    DeliveryError,
    EmailAttachment,
    EmailService,
    EmailTemplate,
    NotificationPayload,
    NotificationService,
    NotificationValidationError,
    SMTPConfig,
)


class FakeSMTP:
    def __init__(self) -> None:
        self.ehlo_calls = 0
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, *, context: object) -> None:
        assert context is not None
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: EmailMessage) -> dict[str, object]:
        self.messages.append(message)
        return {}


def config(**overrides: object) -> SMTPConfig:
    values: dict[str, object] = {
        "host": "smtp.example.com",
        "from_address": "sender@example.com",
        "username": "smtp-user",
        "password": "smtp-password",
    }
    values.update(overrides)
    return SMTPConfig(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_smtp_send_builds_message_and_negotiates_tls() -> None:
    fake = FakeSMTP()
    factory_args: list[tuple[str, int, float, bool]] = []

    def factory(host: str, port: int, timeout: float, use_ssl: bool) -> FakeSMTP:
        factory_args.append((host, port, timeout, use_ssl))
        return fake

    email = EmailService(config(), smtp_factory=factory)
    result = await email.send(
        "recipient@example.com",
        "Subject",
        "Plain body",
        "<strong>HTML body</strong>",
        [EmailAttachment("report.txt", b"contents", "text/plain")],
    )

    assert result.provider_status == "accepted"
    assert factory_args == [("smtp.example.com", 587, 10.0, False)]
    assert fake.ehlo_calls == 2
    assert fake.started_tls
    assert fake.login_args == ("smtp-user", "smtp-password")
    message = fake.messages[0]
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "Subject"
    assert any(part.get_filename() == "report.txt" for part in message.walk())


@pytest.mark.asyncio
async def test_notification_payload_metadata_supplies_html_and_attachments() -> None:
    fake = FakeSMTP()
    email = EmailService(
        config(start_tls=False, username=None, password=None),
        smtp_factory=lambda *_: fake,
    )
    payload = NotificationPayload(
        channel="email",
        recipient="recipient@example.com",
        subject="Payload subject",
        body="Payload body",
        metadata={
            "html": "<p>Payload body</p>",
            "attachments": [{"filename": "data.bin", "content": b"123"}],
        },
    )

    assert await email.send(payload)
    assert not fake.started_tls
    assert fake.login_args is None
    assert any(part.get_filename() == "data.bin" for part in fake.messages[0].walk())


@pytest.mark.asyncio
async def test_idempotent_smtp_dispatch_preserves_attachments_and_rejects_conflicts() -> None:
    fake = FakeSMTP()
    email = EmailService(
        config(start_tls=False, username=None, password=None), smtp_factory=lambda *_: fake
    )
    payload = NotificationPayload(
        "email",
        "recipient@example.com",
        "Receipt",
        "Your receipt is attached.",
        metadata={
            "html": "<p>Your receipt is attached.</p>",
            "attachments": [EmailAttachment("receipt.txt", b"order-456", "text/plain")],
        },
        idempotency_key="receipt-456",
    )
    async with NotificationService(transports={"email": email}) as service:
        assert await service.send(payload)
        assert (await service.send(replace(payload, notification_id="repeated"))).deduplicated
        conflict = await service.send(replace(payload, recipient="someone-else@example.com"))
        assert conflict.error_code == "idempotency_conflict"
        assert len(fake.messages) == 1
        message = fake.messages[0]
        assert message["To"] == "recipient@example.com"
        attachment = next(message.iter_attachments())
        assert attachment.get_filename() == "receipt.txt"
        assert attachment.get_payload(decode=True) == b"order-456"


@pytest.mark.asyncio
async def test_safe_template_rendering_and_missing_value() -> None:
    fake = FakeSMTP()
    email = EmailService(config(), smtp_factory=lambda *_: fake)
    email.register_template(
        "deploy",
        EmailTemplate(subject="Deploy $version", text="Hello $name", html="<p>Hello $name</p>"),
    )

    result = await email.send_template(
        "recipient@example.com", "deploy", {"version": "1.2", "name": "Ada"}
    )

    assert result
    assert fake.messages[0]["Subject"] == "Deploy 1.2"
    with pytest.raises(NotificationValidationError, match="missing email template value"):
        await email.send_template("recipient@example.com", "deploy", {"version": "1.2"})
    with pytest.raises(NotificationValidationError, match="unknown email template"):
        await email.send_template("recipient@example.com", "missing", {})


@pytest.mark.asyncio
async def test_missing_configuration_and_header_injection_fail_early() -> None:
    with pytest.raises(ConfigurationError):
        await EmailService().send("recipient@example.com", "Subject", "Body")
    email = EmailService(config(), smtp_factory=lambda *_: FakeSMTP())
    with pytest.raises(NotificationValidationError):
        await email.send("recipient@example.com", "bad\nsubject", "Body")
    with pytest.raises(NotificationValidationError):
        await email.send("not-an-email", "Subject", "Body")


def test_config_and_attachment_validation() -> None:
    with pytest.raises(ConfigurationError):
        SMTPConfig.from_mapping({"host": "smtp.example.com", "from_address": "a@b.com", "extra": 1})
    with pytest.raises(ConfigurationError):
        config(start_tls=True, use_ssl=True)
    with pytest.raises(ConfigurationError):
        config(password=None)
    with pytest.raises(ConfigurationError, match="requires TLS"):
        config(start_tls=False)
    with pytest.raises(NotificationValidationError):
        EmailAttachment("../secret.txt", b"data")
    with pytest.raises(NotificationValidationError):
        EmailAttachment("bad\nname.txt", b"data")
    with pytest.raises(NotificationValidationError):
        EmailAttachment.from_value("bad")  # type: ignore[arg-type]
    with pytest.raises(NotificationValidationError):
        EmailAttachment.from_value({"filename": "a.txt", "content": b"x", "extra": 1})
    with pytest.raises(NotificationValidationError):
        EmailAttachment.from_value({"filename": "a.txt", "content": None})
    assert EmailAttachment.from_value({"filename": "a.txt", "content": "text"}).content == b"text"


@pytest.mark.parametrize(
    "values",
    [
        {"host": "bad host"},
        {"port": 0},
        {"timeout_seconds": 0},
        {"max_message_bytes": 100},
        {"from_address": "not-an-email"},
    ],
)
def test_smtp_config_rejects_invalid_bounds(values: dict[str, Any]) -> None:
    with pytest.raises((ConfigurationError, NotificationValidationError)):
        config(**values)


def test_mapping_requires_host_and_sender() -> None:
    with pytest.raises(ConfigurationError, match="required"):
        SMTPConfig.from_mapping({})


@pytest.mark.asyncio
async def test_attachment_limit_is_enforced() -> None:
    email = EmailService(config(max_message_bytes=1_024), smtp_factory=lambda *_: FakeSMTP())
    with pytest.raises(NotificationValidationError, match="max_message_bytes"):
        await email.send(
            "recipient@example.com",
            "Subject",
            "Body",
            attachments=[EmailAttachment("large.bin", b"x" * 2_000)],
        )


@pytest.mark.asyncio
async def test_transient_smtp_response_is_retryable() -> None:
    class BusySMTP(FakeSMTP):
        def send_message(self, message: EmailMessage) -> dict[str, object]:
            raise smtplib.SMTPDataError(451, b"busy")

    email = EmailService(config(), smtp_factory=lambda *_: BusySMTP())
    with pytest.raises(DeliveryError) as raised:
        await email.send("recipient@example.com", "Subject", "Body")
    assert raised.value.retryable
    assert raised.value.code == "smtp_response_error"


@pytest.mark.asyncio
async def test_payload_metadata_and_direct_argument_validation() -> None:
    email = EmailService(config(), smtp_factory=lambda *_: FakeSMTP())
    invalid_html = NotificationPayload(
        "email", "recipient@example.com", "Subject", "Body", metadata={"html": 1}
    )
    with pytest.raises(NotificationValidationError, match="html"):
        await email.send(invalid_html)
    invalid_attachments = NotificationPayload(
        "email", "recipient@example.com", "Subject", "Body", metadata={"attachments": "bad"}
    )
    with pytest.raises(NotificationValidationError, match="attachments"):
        await email.send(invalid_attachments)
    with pytest.raises(NotificationValidationError, match="required"):
        await email.send("recipient@example.com")
    with pytest.raises(NotificationValidationError, match="body"):
        await email.send("recipient@example.com", "Subject", "")
    with pytest.raises(NotificationValidationError, match="content_type"):
        await email.send(
            "recipient@example.com",
            "Subject",
            "Body",
            attachments=[EmailAttachment("data.unknown", b"x", "invalid")],
        )
    with pytest.raises(NotificationValidationError, match="template_id"):
        email.register_template("", "Body")
    email.register_template("plain", "Hello $name", subject="Hi $name")
    assert "plain" in email.templates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "smtp_authentication_failed", False),
        (smtplib.SMTPRecipientsRefused({}), "address_rejected", False),
        (OSError("offline"), "smtp_connection_error", True),
    ],
)
async def test_smtp_errors_are_typed_and_sanitized(
    error: Exception, code: str, retryable: bool
) -> None:
    class FailingSMTP(FakeSMTP):
        def send_message(self, message: EmailMessage) -> dict[str, object]:
            raise error

    email = EmailService(config(), smtp_factory=lambda *_: FailingSMTP())
    with pytest.raises(DeliveryError) as raised:
        await email.send("recipient@example.com", "Subject", "Body")
    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert "offline" not in str(raised.value)


@pytest.mark.asyncio
async def test_refused_recipient_is_not_success() -> None:
    class RefusingSMTP(FakeSMTP):
        def send_message(self, message: EmailMessage) -> dict[str, object]:
            return {"recipient@example.com": (550, b"no")}

    email = EmailService(config(), smtp_factory=lambda *_: RefusingSMTP())
    with pytest.raises(DeliveryError) as raised:
        await email.send("recipient@example.com", "Subject", "Body")
    assert raised.value.code == "recipient_refused"


def test_default_smtp_factories_are_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = FakeSMTP()
    secure = FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: secure)
    assert (
        EmailService(config(start_tls=False, username=None, password=None))._create_smtp_client()
        is plain
    )
    assert EmailService(config(start_tls=False, use_ssl=True))._create_smtp_client() is secure
