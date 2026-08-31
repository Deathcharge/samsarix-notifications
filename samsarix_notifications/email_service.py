# SPDX-License-Identifier: MPL-2.0
"""SMTP email transport with safe templates and bounded attachments."""

from __future__ import annotations

import asyncio
import mimetypes
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import PurePath
from string import Template
from typing import Any

from ._validation import is_bounded_int, is_bounded_number
from .errors import ConfigurationError, DeliveryError, NotificationValidationError
from .models import NotificationPayload, TransportResult


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    """Validated SMTP connection settings."""

    host: str
    from_address: str
    port: int = 587
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    start_tls: bool = True
    use_ssl: bool = False
    timeout_seconds: float = 10.0
    max_message_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.host or any(character.isspace() for character in self.host):
            raise ConfigurationError("SMTP host must be a non-empty hostname")
        if not is_bounded_int(self.port, 1, 65_535):
            raise ConfigurationError("SMTP port must be an integer between 1 and 65535")
        _validate_email_address(self.from_address, name="from_address")
        if type(self.start_tls) is not bool or type(self.use_ssl) is not bool:
            raise ConfigurationError("SMTP start_tls and use_ssl must be booleans")
        if self.start_tls and self.use_ssl:
            raise ConfigurationError("SMTP start_tls and use_ssl cannot both be enabled")
        if bool(self.username) != bool(self.password):
            raise ConfigurationError("SMTP username and password must be supplied together")
        if self.username and not (self.start_tls or self.use_ssl):
            raise ConfigurationError("SMTP authentication requires TLS")
        if not is_bounded_number(self.timeout_seconds, 0, 300, exclusive_minimum=True):
            raise ConfigurationError("SMTP timeout_seconds must be between 0 and 300")
        if not is_bounded_int(self.max_message_bytes, 1_024, 50 * 1024 * 1024):
            raise ConfigurationError(
                "SMTP max_message_bytes must be an integer from 1 KiB to 50 MiB"
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> SMTPConfig:
        """Build configuration from a mapping while rejecting unknown keys."""

        allowed = {
            "host",
            "from_address",
            "port",
            "username",
            "password",
            "start_tls",
            "use_ssl",
            "timeout_seconds",
            "max_message_bytes",
        }
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ConfigurationError(f"Unknown SMTP configuration keys: {', '.join(unknown)}")
        try:
            return cls(**dict(values))
        except TypeError as exc:
            raise ConfigurationError("SMTP host and from_address are required") from exc


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """An in-memory email attachment."""

    filename: str
    content: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        if not self.filename or PurePath(self.filename).name != self.filename:
            raise NotificationValidationError("attachment filename must be a basename")
        if any(character in self.filename for character in "\r\n\0"):
            raise NotificationValidationError("attachment filename contains invalid characters")
        if not isinstance(self.content, bytes):
            raise NotificationValidationError("attachment content must be bytes")

    @classmethod
    def from_value(cls, value: EmailAttachment | Mapping[str, Any]) -> EmailAttachment:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise NotificationValidationError(
                "attachments must be EmailAttachment objects or mappings"
            )
        unknown = set(value) - {"filename", "content", "content_type"}
        if unknown:
            raise NotificationValidationError(
                f"unknown attachment keys: {', '.join(sorted(unknown))}"
            )
        content = value.get("content")
        if isinstance(content, str):
            content = content.encode()
        if not isinstance(content, bytes):
            raise NotificationValidationError("attachment content must be bytes or a string")
        return cls(
            filename=str(value.get("filename", "")),
            content=content,
            content_type=value.get("content_type"),
        )


@dataclass(frozen=True, slots=True)
class EmailTemplate:
    """A non-evaluating ``string.Template`` email template."""

    subject: str
    text: str
    html: str | None = None


# The factory is an injection seam for tests and alternate SMTP-compatible
# clients. Runtime method behavior is validated at the call boundary.
SMTPFactory = Callable[[str, int, float, bool], Any]


class EmailService:
    """Deliver email through an operator-supplied SMTP server.

    Blocking standard-library SMTP calls run in a worker thread so the public
    interface remains asynchronous. Secrets, recipients, subjects, and bodies
    are never logged by this library.
    """

    def __init__(
        self,
        config: SMTPConfig | Mapping[str, Any] | None = None,
        *,
        smtp_factory: SMTPFactory | None = None,
    ) -> None:
        self.config = SMTPConfig.from_mapping(config) if isinstance(config, Mapping) else config
        self.templates: dict[str, EmailTemplate] = {}
        self._smtp_factory = smtp_factory

    async def send(
        self,
        to: str | NotificationPayload,
        subject: str | None = None,
        body: str | None = None,
        html: str | None = None,
        attachments: Sequence[EmailAttachment | Mapping[str, Any]] | None = None,
    ) -> TransportResult:
        """Build and send one email, returning the SMTP acknowledgement."""

        if isinstance(to, NotificationPayload):
            payload = to
            to = payload.recipient
            subject = payload.subject
            body = payload.body
            html_value = payload.metadata.get("html")
            if html is None and html_value is not None:
                if not isinstance(html_value, str):
                    raise NotificationValidationError("email metadata html must be a string")
                html = html_value
            attachment_value = payload.metadata.get("attachments")
            if attachments is None and attachment_value is not None:
                if not isinstance(attachment_value, Sequence) or isinstance(
                    attachment_value, (str, bytes)
                ):
                    raise NotificationValidationError(
                        "email metadata attachments must be a sequence"
                    )
                attachments = attachment_value
        if subject is None or body is None:
            raise NotificationValidationError("email subject and body are required")
        if self.config is None:
            raise ConfigurationError("SMTP configuration is required before sending email")

        message = self._build_message(to, subject, body, html, attachments or ())
        await asyncio.to_thread(self._send_message, message)
        return TransportResult(provider_status="accepted")

    async def send_template(
        self,
        to: str,
        template_id: str,
        context: Mapping[str, Any],
    ) -> TransportResult:
        """Render a registered safe template and send it."""

        try:
            template = self.templates[template_id]
        except KeyError as exc:
            raise NotificationValidationError(
                f"unknown email template: {template_id}", code="unknown_template"
            ) from exc
        values = {key: str(value) for key, value in context.items()}
        try:
            subject = Template(template.subject).substitute(values)
            text = Template(template.text).substitute(values)
            html = Template(template.html).substitute(values) if template.html else None
        except KeyError as exc:
            raise NotificationValidationError(
                f"missing email template value: {exc.args[0]}", code="missing_template_value"
            ) from exc
        return await self.send(to, subject, text, html)

    def register_template(
        self,
        template_id: str,
        template: str | EmailTemplate,
        *,
        subject: str = "Notification",
        html: str | None = None,
    ) -> None:
        """Register an in-memory template without expression evaluation."""

        if not template_id or len(template_id) > 128:
            raise NotificationValidationError("template_id must contain 1 to 128 characters")
        value = (
            template
            if isinstance(template, EmailTemplate)
            else EmailTemplate(subject, template, html)
        )
        self.templates[template_id] = value

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        html: str | None,
        attachments: Sequence[EmailAttachment | Mapping[str, Any]],
    ) -> EmailMessage:
        assert self.config is not None
        _validate_email_address(to, name="recipient")
        if not subject or len(subject) > 998 or "\r" in subject or "\n" in subject:
            raise NotificationValidationError(
                "email subject is empty, too long, or contains newlines"
            )
        if not body:
            raise NotificationValidationError("email body must not be empty")

        message = EmailMessage()
        message["From"] = self.config.from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype="html")

        total_attachment_bytes = 0
        for attachment_value in attachments:
            attachment = EmailAttachment.from_value(attachment_value)
            total_attachment_bytes += len(attachment.content)
            if total_attachment_bytes > self.config.max_message_bytes:
                raise NotificationValidationError("email attachments exceed max_message_bytes")
            content_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
            content_type = content_type or "application/octet-stream"
            if "/" not in content_type:
                raise NotificationValidationError("attachment content_type must contain a slash")
            maintype, subtype = content_type.split("/", 1)
            message.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
            )
        if len(message.as_bytes()) > self.config.max_message_bytes:
            raise NotificationValidationError("encoded email exceeds max_message_bytes")
        return message

    def _send_message(self, message: EmailMessage) -> None:
        assert self.config is not None
        try:
            smtp = self._create_smtp_client()
            with smtp:
                smtp.ehlo()
                if self.config.start_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.config.username and self.config.password:
                    smtp.login(self.config.username, self.config.password)
                refused = smtp.send_message(message)
                if refused:
                    raise DeliveryError(
                        "SMTP server refused the recipient",
                        code="recipient_refused",
                        retryable=False,
                    )
        except DeliveryError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise DeliveryError(
                "SMTP authentication failed", code="smtp_authentication_failed"
            ) from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise DeliveryError("SMTP rejected an address", code="address_rejected") from exc
        except smtplib.SMTPResponseException as exc:
            retryable = 400 <= exc.smtp_code < 500
            raise DeliveryError(
                f"SMTP server rejected the message with status {exc.smtp_code}",
                code="smtp_response_error",
                retryable=retryable,
            ) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise DeliveryError(
                "SMTP connection failed", code="smtp_connection_error", retryable=True
            ) from exc

    def _create_smtp_client(self) -> Any:
        assert self.config is not None
        if self._smtp_factory is not None:
            return self._smtp_factory(
                self.config.host,
                self.config.port,
                self.config.timeout_seconds,
                self.config.use_ssl,
            )
        if self.config.use_ssl:
            return smtplib.SMTP_SSL(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout_seconds,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(
            self.config.host,
            self.config.port,
            timeout=self.config.timeout_seconds,
        )


def _validate_email_address(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 320:
        raise NotificationValidationError(f"{name} must be a valid email address")
    if any(character in value for character in "\r\n\0"):
        raise NotificationValidationError(f"{name} contains invalid characters")
    try:
        address = Address(addr_spec=value)
    except (TypeError, ValueError) as exc:
        raise NotificationValidationError(f"{name} must be a valid email address") from exc
    if not address.domain or not address.username:
        raise NotificationValidationError(f"{name} must be a valid email address")
