"""Sending email (welcome, reminders, recovery).

How mail goes out is set in config.py:

    MEETINGHOUSE_MAIL = "smtp"     # send through the SMTP server below
    MEETINGHOUSE_SMTP_HOST, MEETINGHOUSE_SMTP_PORT (default 587),
    MEETINGHOUSE_SMTP_USER, MEETINGHOUSE_SMTP_PASSWORD,
    MEETINGHOUSE_MAIL_FROM          # e.g. "Meetinghouse <hello@meetinghouse.ai>"

Anything else (the default) only records the message, which is what
local testing wants: OUTBOX keeps the most recent ones in memory.
A failure to send is logged, never shown to the person as an error,
since every email here is a courtesy copy of something also on the site.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

OUTBOX = []
log = logging.getLogger("meetinghouse.mail")


def Send(to, subject, body):
    if not to:
        return False
    sender = os.environ.get("MEETINGHOUSE_MAIL_FROM", "Meetinghouse <no-reply@meetinghouse.ai>")
    if os.environ.get("MEETINGHOUSE_MAIL") != "smtp":
        OUTBOX.append({"to": to, "subject": subject, "body": body})
        del OUTBOX[:-200]
        log.info("Email (not sent: MEETINGHOUSE_MAIL isn't 'smtp') to %s: %s", to, subject)
        return True
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        port = int(os.environ.get("MEETINGHOUSE_SMTP_PORT", "587"))
        with smtplib.SMTP(os.environ["MEETINGHOUSE_SMTP_HOST"], port, timeout=20) as smtp:
            if port != 25:
                smtp.starttls()
            if os.environ.get("MEETINGHOUSE_SMTP_USER"):
                smtp.login(os.environ["MEETINGHOUSE_SMTP_USER"], os.environ.get("MEETINGHOUSE_SMTP_PASSWORD", ""))
            smtp.send_message(message)
        return True
    except Exception as exc:  # never break a page because mail failed
        log.exception("Could not send email to %s: %s", to, exc)
        return False
