"""Outgoing email. Three backends: smtp (real), console (logs the message;
local use) and
memory (tests read `outbox`). Sending never raises into a request: a failed
email is logged and counted."""

from __future__ import annotations

import logging
import smtplib
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config

log = logging.getLogger("jobmcp.mail")
outbox: list["Mail"] = []  # memory backend


@dataclass
class Mail:
    to: str
    subject: str
    body: str
    # verify | reset | already_registered  (a label for metrics, not sent)
    purpose: str


def _count(purpose: str, outcome: str) -> None:
    try:
        from . import metrics

        metrics.EMAILS.labels(purpose, outcome).inc()
    except Exception:  # metrics must never break mail
        pass


def _smtp_send(m: Mail) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.MAIL_FROM, m.to, m.subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=False), make_msgid()
    msg.set_content(m.body)
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=20
        )
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
    with server:
        if config.SMTP_SECURITY == "starttls":
            server.starttls()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


def send(m: Mail, wait: bool = False) -> bool:
    """smtp sends on a background thread so a slow mail server can't stall a
    signup request.
    Returns False only if `wait=True` and delivery to the relay failed
    (otherwise failure is just logged and counted)."""
    backend = config.MAIL_BACKEND
    log.debug(
        "sending %s email (backend=%s, wait=%s)", m.purpose, backend, wait
    )
    if backend == "memory":
        outbox.append(m)
        _count(m.purpose, "queued")
    elif backend == "console":
        bar = "=" * 72
        log.warning(
            "\n%s\nEMAIL to %s\nSubject: %s\n\n%s\n%s",
            bar,
            m.to,
            m.subject,
            m.body,
            bar,
        )
        _count(m.purpose, "logged")
    elif backend == "smtp":

        def run() -> bool:
            try:
                _smtp_send(m)
                log.info(
                    "%s email sent via %s:%s",
                    m.purpose,
                    config.SMTP_HOST,
                    config.SMTP_PORT,
                )
                _count(m.purpose, "sent")
                return True
            except Exception:
                log.exception("sending %s email failed", m.purpose)
                _count(m.purpose, "failed")
                return False

        if wait:
            return run()
        threading.Thread(target=run, daemon=True).start()
    else:
        log.error(
            "unknown JOBMCP_MAIL_BACKEND %r; dropping %s email",
            backend,
            m.purpose,
        )
        _count(m.purpose, "failed")
        return False
    return True
