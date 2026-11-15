"""Sending mail: sign-in codes for a hosted service.

A mailer is anything with `send(to, subject, text)`. `SmtpMailer` works with any SMTP provider using
the credentials that provider gives you; `FileMailer` writes messages to a file for development; and
`MemoryMailer` keeps them in a list for tests. The SMTP password is never written to a config file: it
comes from the AISITES_SMTP_PASSWORD environment variable or a file only the operator can read.
"""

import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional, Protocol

from .errors import SiteError

TLS_MODES = ("starttls", "ssl", "none")


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str) -> None:
        ...


class MemoryMailer:
    """Keeps messages in `sent`. For tests."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, to: str, subject: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text})


class FileMailer:
    """Appends each message as one JSON line. For development, when no mail server is set up."""

    def __init__(self, path: Any):
        self.path = Path(path)

    def send(self, to: str, subject: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"to": to, "subject": subject, "text": text}, ensure_ascii=False) + "\n")


class SmtpMailer:
    def __init__(self, host: str, sender: str, port: int = 587, username: str = "", password: str = "",
                 tls: str = "starttls", timeout: float = 20):
        if tls not in TLS_MODES:
            raise SiteError("bad_mail_settings", f"'{tls}' is not a TLS mode.", "Use starttls, ssl or none.")
        if not host or "@" not in sender:
            raise SiteError("bad_mail_settings", "SMTP needs a host and a sender address.",
                            "Example: --smtp-host smtp.resend.com --smtp-sender hello@yourdomain.example")
        self.host, self.port, self.username, self.password = host, int(port), username, password
        self.sender, self.tls, self.timeout = sender, tls, timeout

    def message(self, to: str, subject: str, text: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(text)
        return msg

    def send(self, to: str, subject: str, text: str) -> None:
        try:
            if self.tls == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:
                if self.tls == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(self.message(to, subject, text))
        except (smtplib.SMTPException, OSError) as e:
            # The provider's reply may quote credentials or addresses, so only its kind is passed on.
            raise SiteError("mail_failed", "The message could not be sent.",
                            f"The mail server refused or could not be reached ({type(e).__name__}). "
                            "The host's operator should check the SMTP settings.", status=502)


def password_for(directory: Any) -> str:
    """The SMTP password, from the environment or from `smtp.password` beside the platform's files."""
    if os.environ.get("AISITES_SMTP_PASSWORD"):
        return os.environ["AISITES_SMTP_PASSWORD"]
    path = Path(directory) / "smtp.password"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def from_config(config: Optional[dict], directory: Any) -> Any:
    """A mailer from stored settings such as {"type": "smtp", "host": ..., "sender": ...}."""
    config = config or {}
    kind = config.get("type")
    if kind == "smtp":
        return SmtpMailer(config.get("host", ""), config.get("sender", ""), config.get("port", 587),
                          config.get("username", ""), password_for(directory), config.get("tls", "starttls"))
    if kind == "file":
        return FileMailer(config.get("path") or Path(directory) / "outbox.jsonl")
    raise SiteError("bad_mail_settings", "This host has no mail settings.",
                    "Run `aisites host init` with --smtp-host and --smtp-sender, or --mail-file for development.")
