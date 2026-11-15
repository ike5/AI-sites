import json
import smtplib

import pytest

from aisites import SiteError, mail


class FakeServer:
    """Stands in for smtplib's server classes and records what it is asked to do."""
    log: list = []

    def __init__(self, host, port, **kwargs):
        FakeServer.log.append(("connect", type(self).__name__, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, **kwargs):
        FakeServer.log.append(("starttls",))

    def login(self, username, password):
        FakeServer.log.append(("login", username, password))

    def send_message(self, message):
        FakeServer.log.append(("send", message["From"], message["To"], message["Subject"],
                               message.get_content().strip()))


class FakeSSL(FakeServer):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeServer.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    return FakeServer.log


def test_memory_and_file_mailers(tmp_path):
    memory = mail.MemoryMailer()
    memory.send("ana@example.org", "Hi", "Text")
    assert memory.sent == [{"to": "ana@example.org", "subject": "Hi", "text": "Text"}]
    outbox = tmp_path / "deep" / "outbox.jsonl"
    file = mail.FileMailer(outbox)
    file.send("ana@example.org", "Código", "Tu código: 123456")
    file.send("li@example.org", "Second", "Two")
    lines = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {"to": "ana@example.org", "subject": "Código", "text": "Tu código: 123456"}
    assert len(lines) == 2


def test_smtp_with_starttls_logs_in_and_sends(smtp):
    mailer = mail.SmtpMailer("smtp.resend.com", "hello@sites.example", 587, "resend", "re_secret")
    mailer.send("ana@example.org", "Your code", "123456")
    assert smtp == [("connect", "FakeServer", "smtp.resend.com", 587), ("starttls",),
                    ("login", "resend", "re_secret"),
                    ("send", "hello@sites.example", "ana@example.org", "Your code", "123456")]


def test_smtp_over_ssl_and_without_login(smtp):
    mail.SmtpMailer("mail.example", "a@b.example", 465, tls="ssl").send("x@y.example", "S", "T")
    assert smtp[0] == ("connect", "FakeSSL", "mail.example", 465)
    assert not any(step[0] in ("starttls", "login") for step in smtp)
    smtp.clear()
    mail.SmtpMailer("localhost", "a@b.example", 25, tls="none").send("x@y.example", "S", "T")
    assert [step[0] for step in smtp] == ["connect", "send"]


def test_smtp_failure_is_reported_without_leaking_credentials(smtp, monkeypatch):
    def refuse(self, username, password):
        raise smtplib.SMTPAuthenticationError(535, b"bad key re_secret for resend")
    monkeypatch.setattr(FakeServer, "login", refuse)
    with pytest.raises(SiteError) as e:
        mail.SmtpMailer("smtp.resend.com", "a@b.example", username="resend", password="re_secret") \
            .send("x@y.example", "S", "T")
    assert e.value.code == "mail_failed" and e.value.status == 502
    assert "re_secret" not in e.value.message + e.value.hint and "SMTPAuthenticationError" in e.value.hint


def test_settings_are_checked_and_password_comes_from_outside_the_config(tmp_path, monkeypatch):
    with pytest.raises(SiteError) as e:
        mail.SmtpMailer("smtp.example", "not-an-address")
    assert e.value.code == "bad_mail_settings"
    with pytest.raises(SiteError):
        mail.SmtpMailer("smtp.example", "a@b.example", tls="maybe")
    with pytest.raises(SiteError) as e:
        mail.from_config({}, tmp_path)
    assert "host init" in e.value.hint

    config = {"type": "smtp", "host": "smtp.resend.com", "sender": "a@b.example", "username": "resend"}
    assert "password" not in config
    monkeypatch.delenv("AISITES_SMTP_PASSWORD", raising=False)
    assert mail.from_config(config, tmp_path).password == ""
    (tmp_path / "smtp.password").write_text("from-file\n")
    assert mail.from_config(config, tmp_path).password == "from-file"
    monkeypatch.setenv("AISITES_SMTP_PASSWORD", "from-env")
    assert mail.from_config(config, tmp_path).password == "from-env"
    assert isinstance(mail.from_config({"type": "file"}, tmp_path), mail.FileMailer)
