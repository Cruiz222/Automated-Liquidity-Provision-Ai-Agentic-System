"""Outgoing email. Codes are delivered through a Mailer so the transport can be swapped.

ConsoleMailer (default) logs the message and appends it to a file; nothing leaves the machine and nothing is
exposed over HTTP. SmtpMailer sends through an SMTP server over STARTTLS.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

log = logging.getLogger("app.mail")


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    def __init__(self, outbox_path: str | Path | None = None) -> None:
        self.path = Path(outbox_path) if outbox_path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, to: str, subject: str, body: str) -> None:
        log.info("SIMULATED EMAIL to %s | %s | %s", to, subject, body.replace("\n", " "))
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"To: {to}\nSubject: {subject}\n\n{body}\n{'-' * 60}\n")


class MemoryMailer:
    """Collects messages in a list (for tests)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str, username: str = "", password: str = "") -> None:
        self.host, self.port, self.sender, self.username, self.password = host, port, sender, username, password

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
