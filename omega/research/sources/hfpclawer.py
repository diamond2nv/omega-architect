#!/usr/bin/env python3
"""HfpclawerSource — resilient paper sourcing via hfpclawer CLI.

Uses hfpclawer's local paper_store as an offline-capable backend.
Supports two modes:

1. **Query mode** (instant, offline): ``hfpclawer store search -k KEYWORDS -l N``
   — searches already-downloaded papers in the local SQLite database.
   Returns immediately with no network calls.

2. **Collection-request mode** (async, background): Writes a JSON manifest
   to ``~/.omega/paper_requests/`` that hfpclawer's batch/monitor can pick
   up and process asynchronously — searching HF Papers, downloading PDFs,
   converting to Markdown, and populating the paper_store.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omega.research.knowledge import PaperInfo

logger = logging.getLogger("omega.research.sources.hfpclawer")

# ── Paths ────────────────────────────────────────────────────────

PAPER_REQUEST_DIR = Path.home() / ".omega" / "paper_requests"
DEFAULT_HFPCLAWER_CMD = "hfpclawer"


@dataclass
class PaperCollectionRequest:
    """A manifest for hfpclawer to batch-collect papers on a research topic.

    Written by Omega's KnowledgeProver when online sources fail,
    consumed by ``hfpclawer batch`` or ``hfpclawer full``.
    """

    topic: str
    """Research topic (e.g. 'NV center spin Hamiltonian formalization')."""

    keywords: list[str] = field(default_factory=list)
    """Search keywords for HF Papers / arXiv."""

    max_papers: int = 10
    """Maximum papers to collect."""

    require_pdf: bool = True
    """If True, skip papers without open-access PDF."""

    created_at: str = ""
    """ISO timestamp when this request was created."""

    status: str = "pending"
    """pending | in_progress | completed | failed"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, directory: Path = PAPER_REQUEST_DIR) -> str:
        """Write this request as a JSON manifest file."""
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_topic = "".join(c if c.isalnum() or c in " _-" else "_" for c in self.topic)[:40]
        path = directory / f"request_{timestamp}_{safe_topic}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Paper collection request written: %s", path)
        return str(path)


# ── Source class ─────────────────────────────────────────────────


class HfpclawerSource:
    """Query hfpclawer's local paper_store via CLI.

    All queries go through ``hfpclawer store search``, which reads from
    the local SQLite database — zero network calls, instant results.

    Usage:
        >>> source = HfpclawerSource()
        >>> result = source.search("NV center spin Hamiltonian")
        >>> len(result["papers"])
        3
        >>> result["papers"][0].title
        'Imaging damage in steel using a diamond magnetometer'
    """

    def __init__(
        self,
        hfpclawer_cmd: str = DEFAULT_HFPCLAWER_CMD,
        request_dir: str = "",
        auto_submit_request: bool = True,
    ) -> None:
        self._cmd = hfpclawer_cmd
        self._request_dir = Path(request_dir or PAPER_REQUEST_DIR)
        self._auto_submit = auto_submit_request

    @property
    def available(self) -> bool:
        """True if hfpclawer CLI is installed and paper_store is reachable."""
        try:
            result = subprocess.run(
                [self._cmd, "store", "stats"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def search(
        self,
        theorem_header: str,
        max_results: int = 5,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Search the local paper_store via hfpclawer CLI.

        Args:
            theorem_header: Natural-language research topic or theorem header.
            max_results: Maximum papers to return.

        Returns:
            Dict with keys: ``papers`` (list of PaperInfo), ``lemmas`` (empty),
            ``errors``, ``request_submitted`` (bool — whether a collection
            request was generated for future batch processing).
        """
        result: dict[str, Any] = {
            "papers": [],
            "lemmas": [],
            "errors": [],
            "request_submitted": False,
        }

        if not self.available:
            result["errors"].append("hfpclawer CLI not available")
            return result

        # Extract keywords
        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            # Use the header itself as the search query
            keywords = [theorem_header[:80]]

        try:
            # Query local paper_store via CLI
            cli_result = subprocess.run(
                [self._cmd, "store", "search", "-k", " ".join(keywords), "-l", str(max_results)],
                capture_output=True,
                text=True,
                timeout=15,
            )

            if cli_result.returncode == 0 and cli_result.stdout.strip():
                papers = self._parse_store_output(cli_result.stdout)
                result["papers"] = papers
                logger.info(
                    "hfpclawer returned %d papers for keywords: %s",
                    len(papers),
                    keywords,
                )
            else:
                logger.info(
                    "hfpclawer returned no results for keywords: %s",
                    keywords,
                )
                # No results found — optionally submit a collection request
                if self._auto_submit:
                    request = PaperCollectionRequest(
                        topic=theorem_header[:100],
                        keywords=keywords,
                        max_papers=max_results,
                        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    )
                    request.save(self._request_dir)
                    result["request_submitted"] = True
                    result["errors"].append(
                        f"No papers found; collection request submitted to "
                        f"{self._request_dir}. Run 'hfpclawer batch' to process."
                    )

        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            result["errors"].append(f"hfpclawer query failed: {exc}")

        return result

    @staticmethod
    def _extract_keywords(header: str) -> list[str]:
        """Extract search keywords from a Lean4 theorem header or research topic.

        For research topics (not Lean headers), returns the full text.
        """
        import re

        # Check if it's a Lean theorem header
        name_match = re.match(r"(?:theorem|lemma|def)\s+(\w+)", header)
        if name_match:
            name = name_match.group(1)
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
            parts = [p.lower() for p in parts if len(p) > 2]
            return parts[:5]

        # For research topics, extract meaningful words
        words = header.split()
        # Filter out short/common words
        stop_words = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "are",
            "was",
            "were",
            "can",
            "will",
            "may",
            "but",
            "all",
            "each",
            "its",
            "not",
        }
        keywords = [
            w.strip(",:;.()[]") for w in words if len(w) > 3 and w.lower() not in stop_words
        ]
        return keywords[:5] if keywords else [header[:40]]

    @staticmethod
    def _parse_store_output(stdout: str) -> list[PaperInfo]:
        """Parse hfpclawer store search table output into PaperInfo list.

        Handles both JSON output (if available) and tabular output.
        """
        papers: list[PaperInfo] = []

        # Try JSON first
        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                for item in data:
                    papers.append(
                        PaperInfo(
                            arxiv_id=str(item.get("sf_id", item.get("arxiv_id", ""))),
                            title=str(item.get("title", "")),
                            abstract=str(item.get("abstract", ""))[:500],
                            relevance=float(item.get("relevance", item.get("score", 0))),
                            doi=str(item.get("doi", "")),
                            source_url=f"https://arxiv.org/abs/{item.get('sf_id', '')}"
                            if item.get("sf_id")
                            else "",
                        )
                    )
                return papers
        except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: parse tabular output
        # Format: snowflake_id | title | relevance
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line or "---" in line or "sf_id" in line.lower():
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                papers.append(
                    PaperInfo(
                        arxiv_id=parts[0],
                        title=parts[1],
                        abstract="",
                        relevance=1.0,
                        source_url=f"https://arxiv.org/abs/{parts[0]}" if parts[0] else "",
                    )
                )

        return papers


# ── CLI convenience ─────────────────────────────────────────────


def submit_paper_request(
    topic: str,
    keywords: list[str] | None = None,
    max_papers: int = 10,
) -> str:
    """Submit a paper collection request for hfpclawer batch processing.

    Returns the path to the saved manifest file.
    """
    request = PaperCollectionRequest(
        topic=topic,
        keywords=keywords or [],
        max_papers=max_papers,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    return request.save()
