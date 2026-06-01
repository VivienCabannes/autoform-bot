"""Google Chat bridge client — HTTP operations against the GChat bridge.

No MCP dependencies.
"""

from __future__ import annotations

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30
DEFAULT_SPACES_LIMIT = 20
DEFAULT_MESSAGES_LIMIT = 25


class GChatClient:
    """HTTP client for the GChat API bridge."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        secret: str = "",
        timeout_s: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url
        self.secret = secret
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.secret:
            h["Authorization"] = f"Bearer {self.secret}"
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.request(
                method,
                f"{self.base_url}/{path}",
                headers=self._headers(),
                params=params,
                json=body if method == "POST" and body else None,
            )
            resp.raise_for_status()
            return resp.json()

    async def list_spaces(self, limit: int = DEFAULT_SPACES_LIMIT) -> str:
        data = await self._request("GET", "list_spaces", params={"limit": limit})
        if not data.get("success"):
            return f"Error: {data.get('error', 'unknown error')}"
        spaces = data.get("data", {}).get("spaces", [])
        lines = []
        for s in spaces:
            display = s.get("display_name", "(DM)")
            name = s.get("name", "")
            space_type = s.get("type", "")
            lines.append(f"{name}  |  {display}  |  {space_type}")
        return "\n".join(lines) if lines else "No spaces found."

    async def list_messages(self, space: str, limit: int = DEFAULT_MESSAGES_LIMIT) -> str:
        data = await self._request("GET", "list_messages", params={"space": space, "limit": limit})
        if not data.get("success"):
            return f"Error: {data.get('error', 'unknown error')}"
        messages = data.get("data", {}).get("messages", [])
        lines = []
        for m in messages:
            ts = m.get("create_time", "")
            sender = m.get("sender", "unknown")
            text = m.get("text", "")
            lines.append(f"[{ts}] ({sender}): {text}")
        return "\n".join(lines) if lines else "No messages found."

    async def send_message(self, space: str, message: str) -> str:
        data = await self._request("POST", "send_message", body={"space": space, "message": message})
        if not data.get("success"):
            return f"Error: {data.get('error', 'unknown error')}"
        name = data.get("data", {}).get("name", "")
        return f"Message sent: {name}\nText: {message}"

    async def get_message(self, message_name: str) -> str:
        data = await self._request("GET", "get_message", params={"message_name": message_name})
        if not data.get("success"):
            return f"Error: {data.get('error', 'unknown error')}"
        m = data.get("data", {})
        ts = m.get("create_time", "")
        sender = m.get("sender", "unknown")
        text = m.get("text", "")
        return f"[{ts}] ({sender}): {text}"
