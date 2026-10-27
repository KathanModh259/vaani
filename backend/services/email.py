"""Outbound email: password resets, verification, owner notifications.

Plain SMTP through the standard library, run in a worker thread, so any
provider works (Amazon SES, Resend, Postmark, Zoho, Gmail Workspace) with no
new dependency. With SMTP_HOST unset, messages are logged instead - fine for
development, and the log line includes the body outside production so reset
links can be copied from the console.

Sending never raises into the caller: a mail outage must not fail a signup
or a live call. Use queue_email() for fire-and-forget, send_email() when the
caller wants to know whether it went.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional, Set

from scripts.config import settings

logger = logging.getLogger(__name__)

_background: Set[asyncio.Task] = set()

# Tests read this instead of a mailbox.
outbox: List[EmailMessage] = []


def configured() -> bool:
    return bool(settings.SMTP_HOST)


def _build(to: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    return msg


def _send_blocking(msg: EmailMessage) -> None:
    if settings.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=15) as s:
            if settings.SMTP_USERNAME:
                s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
        if settings.SMTP_STARTTLS:
            s.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        s.send_message(msg)


async def send_email(to: Optional[str], subject: str, text: str) -> bool:
    if not to:
        return False
    msg = _build(to, subject, text)
    outbox.append(msg)
    del outbox[:-50]  # bounded; only tests look at it

    if not configured():
        is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")
        logger.warning("SMTP not configured - email to %s not sent: %s%s", to, subject,
                       "" if is_prod else f"\n{text}")
        return False
    try:
        await asyncio.to_thread(_send_blocking, msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.warning("Email to %s failed (%s): %s", to, subject, e)
        return False


def queue_email(to: Optional[str], subject: str, text: str) -> None:
    """Fire-and-forget; keeps a strong reference so the task is not collected."""
    if not to:
        return
    task = asyncio.create_task(send_email(to, subject, text))
    _background.add(task)
    task.add_done_callback(_background.discard)


def public_url(path: str) -> str:
    base = settings.PUBLIC_BASE_URL or settings.FRONTEND_URL or ""
    return base.rstrip("/") + path
