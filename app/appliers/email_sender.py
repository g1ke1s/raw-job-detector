"""
SMTP email sender using aiosmtplib.

Gmail setup:
  1. Enable 2-Step Verification on your Google account.
  2. Google Account → Security → App Passwords → generate one for "Mail".
  3. Set SMTP_USER=your@gmail.com and SMTP_PASSWORD=<16-char app password> in .env.
  4. SMTP_HOST defaults to smtp.gmail.com, SMTP_PORT defaults to 587 (STARTTLS).

The regular Google account password will NOT work — it must be an App Password.
If you get "Username and Password not accepted", double-check the App Password,
not your regular password.
"""
from __future__ import annotations

import logging
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings

log = logging.getLogger(__name__)


def _smtp_kwargs() -> dict:
    return dict(
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        start_tls=True,
    )


def _check_credentials() -> bool:
    if not settings.smtp_user or not settings.smtp_password:
        log.error(
            "[EMAIL] SMTP_USER or SMTP_PASSWORD not set in .env. "
            "SMTP_PASSWORD must be a Gmail App Password, NOT your regular password."
        )
        return False
    return True


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success, False on any failure."""
    if not _check_credentials():
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    if settings.smtp_bcc:
        msg["Bcc"] = settings.smtp_bcc
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        log.info("[EMAIL] Connecting to %s:%s as %s → %s",
                 settings.smtp_host, settings.smtp_port, settings.smtp_user, to)
        await aiosmtplib.send(msg, **_smtp_kwargs())
        log.info("[EMAIL] Sent OK to %s (subject: %s)", to, subject)
        return True
    except aiosmtplib.SMTPAuthenticationError as e:
        log.error(
            "[EMAIL] Authentication failed: %s\n"
            "Make sure SMTP_PASSWORD is a Gmail App Password (16 chars, no spaces).", e,
        )
    except aiosmtplib.SMTPConnectError as e:
        log.error("[EMAIL] Cannot connect to %s:%s — %s", settings.smtp_host, settings.smtp_port, e)
    except aiosmtplib.SMTPRecipientsRefused as e:
        log.error("[EMAIL] Recipient refused %s: %s", to, e)
    except aiosmtplib.SMTPException as e:
        log.error("[EMAIL] SMTP error sending to %s: %s", to, e)
    except Exception as e:
        log.error("[EMAIL] Unexpected error sending to %s: %s", to, e)
    return False


async def send_email_with_pdf(
    to: str, subject: str, body: str, pdf_bytes: bytes, filename: str = "CV.pdf"
) -> bool:
    """Send email with a plain-text body and a PDF attachment."""
    if not _check_credentials():
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    if settings.smtp_bcc:
        msg["Bcc"] = settings.smtp_bcc
    msg.attach(MIMEText(body, "plain", "utf-8"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    try:
        log.info("[EMAIL] Sending with PDF attachment to %s (subject: %s)", to, subject)
        await aiosmtplib.send(msg, **_smtp_kwargs())
        log.info("[EMAIL] Sent with PDF OK to %s", to)
        return True
    except aiosmtplib.SMTPAuthenticationError as e:
        log.error(
            "[EMAIL] Authentication failed: %s\n"
            "Make sure SMTP_PASSWORD is a Gmail App Password (16 chars, no spaces).", e,
        )
    except aiosmtplib.SMTPConnectError as e:
        log.error("[EMAIL] Cannot connect to %s:%s — %s", settings.smtp_host, settings.smtp_port, e)
    except aiosmtplib.SMTPRecipientsRefused as e:
        log.error("[EMAIL] Recipient refused %s: %s", to, e)
    except aiosmtplib.SMTPException as e:
        log.error("[EMAIL] SMTP error sending to %s: %s", to, e)
    except Exception as e:
        log.error("[EMAIL] Unexpected error sending to %s: %s", to, e)
    return False
