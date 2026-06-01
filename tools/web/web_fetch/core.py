"""Web Fetch — URL fetching with HTML-to-markdown conversion.

No MCP dependencies. Uses httpx for HTTP requests.
"""

from __future__ import annotations

import re

import httpx

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_CONTENT_LENGTH = 50_000
DEFAULT_USER_AGENT = "Fort-Agent/1.0"


class WebFetcher:
    """Fetch URLs and convert HTML content to markdown."""

    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT, max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH) -> None:
        self.timeout_s = timeout_s
        self.max_content_length = max_content_length

    @staticmethod
    def _html_to_markdown(html: str) -> str:
        """Best-effort HTML-to-markdown conversion without extra dependencies."""
        # Strip script/style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Convert common tags
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</?p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(
            r"<h([1-6])[^>]*>(.*?)</h\1>",
            lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.IGNORECASE)
        text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.IGNORECASE)
        text = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.IGNORECASE)
        text = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.IGNORECASE)
        text = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode common entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def fetch(self, url: str) -> str:
        """Fetch a URL and return its content as markdown.

        HTTP URLs are auto-upgraded to HTTPS. Returns the page content
        converted from HTML to markdown.
        """
        if url.startswith("http://"):
            url = "https://" + url[7:]

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout_s,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            body = resp.text[: self.max_content_length]

            if "html" in content_type:
                body = self._html_to_markdown(body)

            if len(body) > self.max_content_length:
                body = body[: self.max_content_length] + "\n\n[Content truncated]"

            return f"URL: {url}\nStatus: {resp.status_code}\n\n{body}"
