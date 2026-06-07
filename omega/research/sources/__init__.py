#!/usr/bin/env python3
"""Research data sources for Omega's Phase 0 knowledge pipeline.

Each source implements ``search(theorem_header: str, **kwargs) -> dict``
returning a dict with at least ``{papers?, lemmas?, errors?}``.
"""

from omega.research.sources.arxiv import ArxivSource
from omega.research.sources.hfpclawer import (
    HfpclawerSource,
    PaperCollectionRequest,
    submit_paper_request,
)
from omega.research.sources.kiwix import KiwixSource
from omega.research.sources.leancode import LeanCodeSource
from omega.research.sources.paperstore import PaperStoreSource
from omega.research.sources.wiki import WikiSource

__all__ = [
    "ArxivSource",
    "KiwixSource",
    "LeanCodeSource",
    "PaperStoreSource",
    "WikiSource",
]
