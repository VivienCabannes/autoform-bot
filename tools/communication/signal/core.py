"""Signal Messenger client — HTTP operations against the signal-cli-rest-api.

No MCP dependencies.
"""

from __future__ import annotations

import httpx


class SignalClient:
    """Async HTTP client for the signal-cli-rest-api Docker container."""

    def __init__(
        self,
        base_url: str = "http://localhost:9922",
        sender_number: str = "",
        allowed_group_names: list[str] | None = None,
        timeout_s: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sender_number = sender_number
        self.allowed_group_names: list[str] = allowed_group_names or []
        self.timeout_s = timeout_s
        # Lazily populated mappings
        self._name_to_id: dict[str, str] = {}  # name -> external id (for sending)
        self._internal_id_to_name: dict[str, str] = {}  # internal_id -> name (for receiving)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Content-Type": "application/json"},
                params=params,
                json=body if method == "POST" and body else None,
            )
            resp.raise_for_status()
            return resp

    async def _resolve_groups(self) -> None:
        """Fetch all groups and build name/ID mappings."""
        resp = await self._request("GET", f"/v1/groups/{self.sender_number}")
        groups = resp.json()
        self._name_to_id = {}
        self._internal_id_to_name = {}
        for g in groups:
            name = g.get("name", "")
            gid = g.get("id", "")
            internal_id = g.get("internal_id", "")
            if name and gid:
                self._name_to_id[name] = gid
            if name and internal_id:
                self._internal_id_to_name[internal_id] = name

    async def _get_group_id(self, group_name: str) -> str | None:
        """Resolve a group name to its ID, fetching from API if needed."""
        if not self._name_to_id:
            await self._resolve_groups()
        return self._name_to_id.get(group_name)

    async def list_groups(self) -> str:
        """List Signal groups, filtered to allowed group names if configured."""
        await self._resolve_groups()

        items = self._name_to_id.items()
        if self.allowed_group_names:
            items = [(n, gid) for n, gid in items if n in self.allowed_group_names]

        if not items:
            return "No groups found."

        lines = []
        for name, _gid in items:
            lines.append(name)
        return "Available groups:\n" + "\n".join(f"  - {line}" for line in lines)

    async def send_message(self, group_name: str, message: str) -> str:
        """Send a text message to a Signal group by name."""
        if self.allowed_group_names and group_name not in self.allowed_group_names:
            return f"Error: Group '{group_name}' is not in the allowed groups list."

        group_id = await self._get_group_id(group_name)
        if not group_id:
            return f"Error: No group found with name '{group_name}'."

        body = {
            "message": message,
            "number": self.sender_number,
            "recipients": [group_id],
        }
        resp = await self._request("POST", "/v2/send", body=body)
        data = resp.json()

        if isinstance(data, list):
            errors = [r for r in data if r.get("error")]
            if errors:
                return f"Send errors: {errors}"
        return f"Message sent to '{group_name}'."

    async def receive_messages(self) -> str:
        """Fetch pending messages for the registered number."""
        if not self._internal_id_to_name:
            await self._resolve_groups()

        resp = await self._request("GET", f"/v1/receive/{self.sender_number}")
        messages = resp.json()

        if not messages:
            return "No new messages."

        lines = []
        for m in messages:
            envelope = m.get("envelope", {})
            source = envelope.get("sourceName") or envelope.get("sourceNumber", "unknown")
            timestamp = envelope.get("timestamp", "")
            data_msg = envelope.get("dataMessage", {})
            text = data_msg.get("message", "")
            group_info = data_msg.get("groupInfo", {})
            group_internal_id = group_info.get("groupId", "")

            group_name = self._internal_id_to_name.get(group_internal_id, "")

            # Filter to allowed groups if configured
            if self.allowed_group_names and group_name and group_name not in self.allowed_group_names:
                continue

            if text:
                prefix = f"[{group_name}] " if group_name else ""
                lines.append(f"[{timestamp}] {prefix}({source}): {text}")

        return "\n".join(lines) if lines else "No new messages."
