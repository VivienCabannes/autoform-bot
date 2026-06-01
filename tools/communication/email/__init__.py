"""Email tool — IMAP/SMTP email operations."""

from .core import EmailClient
from .server import create_email_server, email_server

__all__ = ["EmailClient", "create_email_server", "email_server"]
