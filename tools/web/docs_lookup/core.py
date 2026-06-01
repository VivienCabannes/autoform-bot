"""Library Documentation Lookup — web-based documentation search and fetch.

No MCP dependencies. Uses httpx for HTTP requests.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

import httpx

DEFAULT_TIMEOUT = 20
DEFAULT_USER_AGENT = "Fort-Agent/1.0"
DEFAULT_MAX_SEARCH_BLOCKS = 5
DEFAULT_MAX_LENGTH = 50_000


class DocsLookup:
    """Search and fetch external library documentation."""

    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT) -> None:
        self.timeout_s = timeout_s

    async def _fetch_url(self, url: str) -> str:
        """Fetch a URL and return content as text."""
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            resp.raise_for_status()
            return resp.text

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Strip HTML tags for plain-text extraction."""
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def search_docs(self, library: str, query: str) -> str:
        """Search for library documentation on the web.

        Args:
            library: Library/package name (e.g. "react", "fastapi", "numpy").
            query: What you want to know (e.g. "how to use hooks", "async endpoints").

        Returns:
            Search results with links to relevant documentation.
        """
        search_query = f"{library} documentation {query}"
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}"

        try:
            html = await self._fetch_url(url)
        except httpx.HTTPError as e:
            return f"Search failed: {e}"

        results: list[str] = []
        blocks = re.findall(r'<div class="result[^"]*".*?</div>\s*</div>', html, re.DOTALL)

        for block in blocks[:DEFAULT_MAX_SEARCH_BLOCKS]:
            title_match = re.search(r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
            url_match = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', block)
            snippet_match = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)

            if title_match and url_match:
                title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
                result_url = url_match.group(1)
                snippet = ""
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                results.append(f"- [{title}]({result_url})")
                if snippet:
                    results.append(f"  {snippet}")

        if not results:
            return f"No documentation found for '{library}' with query '{query}'"

        return f"Documentation results for {library} — {query}:\n\n" + "\n".join(results)

    async def fetch_doc_page(self, url: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
        """Fetch and extract content from a documentation page.

        Args:
            url: URL of the documentation page.
            max_length: Maximum characters to return.

        Returns:
            Extracted text content from the page.
        """
        try:
            html = await self._fetch_url(url)
        except httpx.HTTPError as e:
            return f"Fetch failed: {e}"

        text = self._html_to_text(html)
        if len(text) > max_length:
            text = text[:max_length] + "\n\n[Content truncated]"
        return f"Source: {url}\n\n{text}"
