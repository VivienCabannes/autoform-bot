"""Web Search — DuckDuckGo HTML scrape search backend.

No MCP dependencies. Uses httpx for HTTP requests.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

import httpx

DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RESULTS = 10
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; Fort-Agent/1.0)"


class WebSearcher:
    """Web search via DuckDuckGo HTML scrape (no API key needed)."""

    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self.timeout_s = timeout_s
        self.max_results = max_results

    async def search(self, query: str) -> str:
        """Search the web and return results.

        Returns a list of results with title, URL, and snippet.
        """
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                },
            )
            resp.raise_for_status()
            html = resp.text

        # Parse result blocks from DDG HTML response
        results: list[dict[str, str]] = []
        # Each result is in a <div class="result ..."> block
        result_blocks = re.findall(
            r'<div class="result[^"]*".*?</div>\s*</div>',
            html,
            re.DOTALL,
        )

        for block in result_blocks[: self.max_results]:
            # Extract title and URL
            title_match = re.search(r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
            url_match = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', block)
            snippet_match = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)

            if title_match and url_match:
                title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
                result_url = url_match.group(1)
                snippet = ""
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()

                results.append({"title": title, "url": result_url, "snippet": snippet})

        if not results:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['title']}]({r['url']})")
            if r["snippet"]:
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines)
