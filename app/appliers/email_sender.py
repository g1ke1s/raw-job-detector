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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings

log = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success, False on any failure."""
    if not settings.smtp_user or not settings.smtp_password:
        log.error(
            "[EMAIL] SMTP_USER or SMTP_PASSWORD not set in .env. "
            "SMTP_PASSWORD must be a Gmail App Password, NOT your regular password."
        )
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
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=True,
        )
        log.info("[EMAIL] Sent OK to %s (subject: %s)", to, subject)
        return True

    except aiosmtplib.SMTPAuthenticationError as e:
        log.error(
            "[EMAIL] Authentication failed: %s\n"
            "Make sure SMTP_PASSWORD is a Gmail App Password (16 chars, no spaces), "
            "not your regular account password.",
            e,
        )
    except aiosmtplib.SMTPConnectError as e:
        log.error("[EMAIL] Cannot connect to %s:%s — %s",
                  settings.smtp_host, settings.smtp_port, e)
    except aiosmtplib.SMTPRecipientsRefused as e:
        log.error("[EMAIL] Recipient refused %s: %s", to, e)
    except aiosmtplib.SMTPException as e:
        log.error("[EMAIL] SMTP error sending to %s: %s", to, e)
    except Exception as e:
        log.error("[EMAIL] Unexpected error sending to %s: %s", to, e)

    return False
