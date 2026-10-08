"""The email boundary (P3b-D, 04 §3.5, 11 §0 finding 3).

Plain SMTP via the standard library. In dev it points at the Mailpit
container compose already runs (`localhost:1025`, UI on `:8025`), so dev email
is *real* email into a sink, not a fake. In production the same adapter points
at Postmark's or SES's SMTP endpoint with credentials. Only tests, and a box
that explicitly sets `email_backend=fake`, use the fake.

Named `mailer` rather than `email` so it can never be mistaken for, or shadow,
the standard library's `email` package it builds messages with.
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

from app.config import settings
from app.services.sms import SendFailed

logger = logging.getLogger("apichain.mailer")


class EmailSender(Protocol):
    provider: str

    def send(self, to: str, subject: str, body: str) -> str:
        """Send and return the Message-ID."""
        ...


class SmtpEmail:
    provider = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None,
        password: str | None,
        starttls: bool,
        timeout: float,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._username, self._password = username, password
        self._starttls, self._timeout = starttls, timeout

    def send(self, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message_id = make_msgid(domain=self._sender.rpartition("@")[2] or None)
        message["Message-ID"] = message_id
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                if self._starttls:
                    smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password or "")
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("smtp send failed: %s", type(exc).__name__)
            raise SendFailed("provider_unreachable") from exc
        return message_id


def from_settings() -> EmailSender:
    if settings.email_backend == "fake":
        from app.services.dev_fakes import LogEmail

        return LogEmail()
    return SmtpEmail(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        timeout=settings.smtp_timeout_seconds,
    )
