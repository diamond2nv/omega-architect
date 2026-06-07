#!/usr/bin/env python3
"""KiwixSource — query offline Wikipedia via Kiwix ZIM server.

Connects to a local Kiwix HTTP server (typically localhost:8090) to
search offline Wikipedia content for physics/math definitions,
theorem statements, and formula references.  Gracefully returns empty
results when Kiwix is not running.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("omega.research.sources.kiwix")

# ── Default ports to probe ─────────────────────────────────────

DEFAULT_PORTS = [8090, 8080]


# ── Kiwix Source ───────────────────────────────────────────────


class KiwixSource:
    """Query a local Kiwix ZIM server for Wikipedia content.

    Uses the Kiwix search API (``/search?content=...&pattern=...``) to
    find articles matching theorem keywords.  When no Kiwix server is
    available, returns empty results silently.

    Usage:
        >>> source = KiwixSource()
        >>> result = source.search("Maxwell equations")
        >>> result["articles"]
        [{"title": "Maxwell's equations", "url": "...", "snippet": "..."}]
    """

    def __init__(self, base_url: str = "") -> None:
        self._base_url = base_url or self._discover()

    @staticmethod
    def _discover() -> str:
        """Auto-discover Kiwix server by probing common ports."""
        for port in DEFAULT_PORTS:
            url = f"http://localhost:{port}"
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return url
            except Exception:
                continue
        return ""

    @property
    def available(self) -> bool:
        """True if Kiwix server is reachable (live probe)."""
        if not self._base_url:
            return False
        try:
            req = urllib.request.Request(self._base_url, method="HEAD")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ── Search API ────────────────────────────────────────────

    def search(
        self,
        query: str,
        max_results: int = 3,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search Kiwix Wikipedia for articles matching *query*.

        Args:
            query: Free-text search query (physics/math terms).
            max_results: Max articles to return.

        Returns:
            Dict with keys:
                ``articles`` — list of ``{title, url, snippet}``
                ``n_articles`` — count
                ``errors`` — errors if any
        """
        result: dict[str, Any] = {
            "articles": [],
            "n_articles": 0,
            "errors": [],
        }

        if not self.available:
            return result

        try:
            articles = self._search_kiwix(query, max_results)
            result["articles"] = articles
            result["n_articles"] = len(articles)
        except Exception as exc:
            result["errors"].append(f"Kiwix search failed: {exc}")
            logger.debug("KiwixSource error: %s", exc)

        return result

    def _search_kiwix(self, query: str, max_results: int) -> list[dict[str, Any]]:
        """Execute a Kiwix search via HTTP API."""
        params = urllib.parse.urlencode(
            {
                "pattern": query,
                "maxResults": str(max_results),
            }
        )
        url = f"{self._base_url}/search?{params}"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        return self._parse_search_results(html, max_results)

    @staticmethod
    def _parse_search_results(html: str, max_results: int) -> list[dict[str, Any]]:
        """Parse Kiwix search result HTML into structured results.

        Kiwix returns HTML with ``<a>`` links and ``<p>`` snippets.
        We extract title, URL path, and snippet text.
        """
        articles: list[dict[str, Any]] = []

        # Find result items — look for <a href="/A/Article_Title">...</a>
        # in the content area.
        link_pattern = re.compile(
            r'<a\s+href="(/(?:A|I|content)/[^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        for match in link_pattern.finditer(html):
            url_path = match.group(1)
            title_raw = re.sub(r"<[^>]+>", "", match.group(2)).strip()

            if not title_raw:
                continue

            # Extract snippet from surrounding paragraph
            snippet_start = max(0, match.start() - 200)
            snippet_end = min(len(html), match.end() + 200)
            snippet_area = html[snippet_start:snippet_end]
            snippet = re.sub(r"<[^>]+>", "", snippet_area).strip()[:200]

            articles.append(
                {
                    "title": title_raw,
                    "url": url_path,
                    "snippet": snippet,
                }
            )

            if len(articles) >= max_results:
                break

        return articles

    # ── Domain-specific lookups ───────────────────────────────

    def lookup_physics_formula(self, formula_name: str) -> str | None:
        """Look up a specific physics formula by name.

        Args:
            formula_name: e.g. "Maxwell's equations", "Schrödinger equation"

        Returns:
            Wikipedia article text or None if not found.
        """
        result = self.search(formula_name, max_results=1)
        articles = result.get("articles", [])
        if not articles:
            return None

        # Fetch the article content
        return self._fetch_article(articles[0]["url"])

    def _fetch_article(self, url_path: str) -> str | None:
        """Fetch a full article page from Kiwix."""
        if not self.available:
            return None

        try:
            url = f"{self._base_url}{url_path}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # Strip HTML tags, keep text
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:2000]  # limit to first 2000 chars
        except Exception as exc:
            logger.debug("Kiwix fetch failed: %s", exc)
            return None
