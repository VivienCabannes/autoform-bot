"""Email operations — IMAP/SMTP helpers, parsing, authorization.

No MCP dependencies. Connections are created per-call to avoid stale connection issues.
"""

from __future__ import annotations

import email.utils
import imaplib
import json
import mimetypes
import os
import smtplib
from email import encoders, policy
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser

DEFAULT_SNIPPET_LENGTH = 200
DEFAULT_IMAP_SERVER = "imap.gmail.com"
DEFAULT_SMTP_SERVER = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587


def extract_address(sender: str) -> str:
    """Extract bare email address from a From header value."""
    _, addr = email.utils.parseaddr(sender)
    return addr.lower()


def is_authorized_sender(sender: str, authorized: list[str]) -> bool:
    """Check if sender is in the authorized list."""
    addr = extract_address(sender)
    return any(a.lower() == addr for a in authorized)


def get_email_body(msg) -> str:
    """Extract plain text body from a parsed email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = part.get_content_disposition()
            if ctype == "text/plain" and "attachment" not in (cdispo or ""):
                return part.get_content()
    else:
        return msg.get_content()
    return ""


def parse_email_summary(msg, email_id: str) -> dict:
    """Parse an email into a summary dict."""
    body = get_email_body(msg)
    snippet = body[:DEFAULT_SNIPPET_LENGTH].strip() if body else ""
    return {
        "id": email_id,
        "from": msg["From"],
        "subject": msg["Subject"],
        "date": msg["Date"],
        "snippet": snippet,
    }


class EmailClient:
    """IMAP/SMTP email operations scoped to a bot account."""

    def __init__(
        self,
        bot_email: str,
        bot_password: str,
        imap_server: str = DEFAULT_IMAP_SERVER,
        smtp_server: str = DEFAULT_SMTP_SERVER,
        smtp_port: int = DEFAULT_SMTP_PORT,
        authorized_senders: list[str] | None = None,
    ) -> None:
        self.bot_email = bot_email
        self.bot_password = bot_password
        self.imap_server = imap_server
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.authorized_senders = authorized_senders

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        mail = imaplib.IMAP4_SSL(self.imap_server)
        mail.login(self.bot_email, self.bot_password)
        return mail

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        server = smtplib.SMTP(self.smtp_server, self.smtp_port)
        server.starttls()
        server.login(self.bot_email, self.bot_password)
        server.send_message(msg)
        server.quit()

    def read_inbox(self, limit: int = 10, unread_only: bool = True) -> str:
        mail = self._imap_connect()
        try:
            mail.select("inbox")
            criterion = "UNSEEN" if unread_only else "ALL"
            _, messages = mail.search(None, criterion)
            email_ids = messages[0].split()[-limit:]

            results = []
            for eid in email_ids:
                _, data = mail.fetch(eid, "(BODY.PEEK[])")
                raw = data[0][1]
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                if self.authorized_senders and not is_authorized_sender(msg["From"], self.authorized_senders):
                    continue
                results.append(parse_email_summary(msg, eid.decode()))
        finally:
            mail.logout()
        return json.dumps(results, indent=2)

    def get_email(self, email_id: str) -> str:
        mail = self._imap_connect()
        try:
            mail.select("inbox")
            _, data = mail.fetch(email_id, "(RFC822)")
            raw = data[0][1]
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        finally:
            mail.logout()

        body = get_email_body(msg)
        return json.dumps(
            {
                "id": email_id,
                "from": msg["From"],
                "to": msg["To"],
                "subject": msg["Subject"],
                "date": msg["Date"],
                "body": body,
            },
            indent=2,
        )

    def search_emails(self, query: str) -> str:
        mail = self._imap_connect()
        try:
            mail.select("inbox")
            _, messages = mail.search(None, query)
            email_ids = messages[0].split()

            results = []
            for eid in email_ids:
                _, data = mail.fetch(eid, "(BODY.PEEK[])")
                raw = data[0][1]
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                if self.authorized_senders and not is_authorized_sender(msg["From"], self.authorized_senders):
                    continue
                results.append(parse_email_summary(msg, eid.decode()))
        finally:
            mail.logout()
        return json.dumps(results, indent=2)

    def _attach_files(self, msg: MIMEMultipart, attachments: list[str]) -> list[str]:
        """Attach files to a MIME message. Returns list of errors (empty = all ok)."""
        errors = []
        for filepath in attachments:
            if not os.path.isfile(filepath):
                errors.append(f"Attachment not found: {filepath}")
                continue
            ctype, encoding = mimetypes.guess_type(filepath)
            if ctype is None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(filepath, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = os.path.basename(filepath)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
        return errors

    def send_email(self, to: str, subject: str, body: str, attachments: list[str] | None = None) -> str:
        if self.authorized_senders and to.lower() not in [a.lower() for a in self.authorized_senders]:
            return f"Error: {to} is not in the authorized senders list."

        msg = MIMEMultipart()
        msg["From"] = self.bot_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if attachments:
            errors = self._attach_files(msg, attachments)
            if errors:
                return "Error attaching files: " + "; ".join(errors)

        self._smtp_send(msg)
        return f"Email sent to {to} with subject '{subject}'."

    def reply_to_email(self, email_id: str, body: str, attachments: list[str] | None = None) -> str:
        mail = self._imap_connect()
        try:
            mail.select("inbox")
            _, data = mail.fetch(email_id, "(RFC822)")
            raw = data[0][1]
            original = BytesParser(policy=policy.default).parsebytes(raw)
        finally:
            mail.logout()

        sender = original["From"]
        recipient = extract_address(sender)
        subject = original["Subject"] or ""

        if self.authorized_senders and recipient not in [a.lower() for a in self.authorized_senders]:
            return f"Error: {recipient} is not in the authorized senders list."

        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        msg = MIMEMultipart()
        msg["From"] = self.bot_email
        msg["To"] = recipient
        msg["Subject"] = reply_subject
        msg.attach(MIMEText(body, "plain"))

        if attachments:
            errors = self._attach_files(msg, attachments)
            if errors:
                return "Error attaching files: " + "; ".join(errors)

        self._smtp_send(msg)
        return f"Reply sent to {recipient} with subject '{reply_subject}'."
