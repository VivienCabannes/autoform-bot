"""Email MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import os
import re

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import EmailClient


def create_email_server(
    bot_email: str,
    bot_password: str,
    *,
    imap_server: str = "imap.gmail.com",
    smtp_server: str = "smtp.gmail.com",
    smtp_port: int = 587,
    authorized_senders: list[str] | None = None,
) -> FastMCP:
    """Create a FastMCP server with email tools."""
    client = EmailClient(
        bot_email,
        bot_password,
        imap_server=imap_server,
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        authorized_senders=authorized_senders,
    )
    server = FastMCP(name="email")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def read_inbox(limit: int = 10, unread_only: bool = True) -> str:
        """Read emails from the inbox.

        Args:
            limit: Maximum number of emails to return.
            unread_only: If True, only return unread emails.

        Returns:
            JSON list of email summaries with id, from, subject, date, snippet.
        """
        return client.read_inbox(limit=limit, unread_only=unread_only)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_email(email_id: str) -> str:
        """Get the full content of an email by its ID.

        Args:
            email_id: The email ID from read_inbox or search_emails.

        Returns:
            JSON with full email metadata and body.
        """
        return client.get_email(email_id)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def search_emails(query: str) -> str:
        """Search emails using IMAP search syntax.

        Args:
            query: IMAP search query (e.g. 'SUBJECT "meeting"', 'FROM "alice@example.com"').

        Returns:
            JSON list of matching email summaries.
        """
        return client.search_emails(query)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def send_email(to: str, subject: str, body: str, attachments: list[str] | None = None) -> str:
        """Send an email, optionally with file attachments.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain text email body.
            attachments: Optional list of absolute file paths to attach (e.g. PDF files).
        """
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
            return "Error: invalid recipient email address"
        return client.send_email(to, subject, body, attachments=attachments)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def reply_to_email(email_id: str, body: str, attachments: list[str] | None = None) -> str:
        """Reply to an email by its ID, optionally with file attachments.

        Args:
            email_id: The email ID to reply to.
            body: Plain text reply body.
            attachments: Optional list of absolute file paths to attach (e.g. PDF files).
        """
        if not email_id.strip():
            return "Error: email_id is required"
        return client.reply_to_email(email_id, body, attachments=attachments)

    return server


def email_server(
    *,
    bot_email: str = "",
    bot_password: str = "",
    imap_server: str = "imap.gmail.com",
    smtp_server: str = "smtp.gmail.com",
    smtp_port: int = 587,
    authorized_senders: list[str] | None = None,
) -> MCPServerConfig:
    """Create an email MCP server config.

    Reads EMAIL_BOT_ADDRESS, EMAIL_BOT_PASSWORD, and EMAIL_AUTHORIZED_SENDERS
    from environment variables as defaults.
    """
    if not bot_email:
        bot_email = os.environ.get("EMAIL_BOT_ADDRESS", "")
    if not bot_password:
        bot_password = os.environ.get("EMAIL_BOT_PASSWORD", "")
    if authorized_senders is None:
        env_senders = os.environ.get("EMAIL_AUTHORIZED_SENDERS", "")
        if env_senders:
            authorized_senders = [s.strip() for s in env_senders.split(",") if s.strip()]

    mcp_instance = create_email_server(
        bot_email,
        bot_password,
        imap_server=imap_server,
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        authorized_senders=authorized_senders,
    )
    return MCPServerConfig(
        server_key="email",
        description="Email sending and receiving",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
