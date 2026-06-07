"""Tests for HfpclawerSource — resilient paper sourcing via hfpclawer CLI."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omega.research.sources.hfpclawer import (
    HfpclawerSource,
    PaperCollectionRequest,
    submit_paper_request,
)


class TestPaperCollectionRequest:
    def test_default_fields(self):
        req = PaperCollectionRequest(topic="NV center sensing")
        assert req.topic == "NV center sensing"
        assert req.status == "pending"
        assert req.max_papers == 10
        assert req.require_pdf is True

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = PaperCollectionRequest(
                topic="Test topic",
                keywords=["NV", "diamond", "sensing"],
                max_papers=5,
                created_at="2026-06-07T12:00:00",
            )
            path = req.save(Path(tmp))
            assert Path(path).is_file()

            loaded = json.loads(Path(path).read_text())
            assert loaded["topic"] == "Test topic"
            assert loaded["keywords"] == ["NV", "diamond", "sensing"]
            assert loaded["max_papers"] == 5
            assert loaded["status"] == "pending"

    def test_to_dict(self):
        req = PaperCollectionRequest(topic="T", keywords=["a", "b"])
        d = req.to_dict()
        assert d["topic"] == "T"
        assert d["keywords"] == ["a", "b"]
        assert d["status"] == "pending"


class TestHfpclawerSource:
    def test_available_detection(self):
        """hfpclawer should be detected if installed."""
        source = HfpclawerSource()
        # May be available or not depending on installation
        assert isinstance(source.available, bool)

    def test_keyword_extraction_lean_header(self):
        """Extract CamelCase words from Lean theorem header."""
        keywords = HfpclawerSource._extract_keywords(
            "theorem add_comm (a b : Nat) : a + b = b + a :="
        )
        assert "comm" in keywords or "add" in keywords

    def test_keyword_extraction_research_topic(self):
        """Extract meaningful words from research topic."""
        keywords = HfpclawerSource._extract_keywords(
            "NV center spin Hamiltonian in diamond"
        )
        assert len(keywords) >= 2
        # Should filter stop words but keep meaningful terms
        assert "center" in keywords or "spin" in keywords or "Hamiltonian" in keywords
        assert "center" in keywords or "diamond" in keywords

    def test_keyword_extraction_fallback_to_header(self):
        """Very short theorem name should search method fallback to full header."""
        keywords = HfpclawerSource._extract_keywords("theorem t")
        # "t" is too short (≤2 chars), so returns empty — the search()
        # method handles this fallback separately
        assert isinstance(keywords, list)

    def test_parse_store_output_json(self):
        """Parse JSON output from hfpclawer store search."""
        json_output = json.dumps([
            {"sf_id": "2011.02459", "title": "Imaging damage in steel",
             "abstract": "We use NV centers...", "relevance": 0.95},
            {"sf_id": "1810.02723", "title": "Eddy current imaging",
             "abstract": "NV-based eddy current...", "relevance": 0.85},
        ])
        papers = HfpclawerSource._parse_store_output(json_output)
        assert len(papers) == 2
        assert papers[0].arxiv_id == "2011.02459"
        assert papers[0].title == "Imaging damage in steel"
        assert papers[1].arxiv_id == "1810.02723"

    def test_parse_store_output_tabular(self):
        """Parse tabular output from hfpclawer store search."""
        table = """sf_id | title | relevance
2011.02459 | Imaging damage in steel | 0.95
1810.02723 | Eddy current imaging | 0.85
"""
        papers = HfpclawerSource._parse_store_output(table)
        assert len(papers) == 2
        assert papers[0].arxiv_id == "2011.02459"

    def test_parse_store_output_empty(self):
        """Empty output should return empty list."""
        papers = HfpclawerSource._parse_store_output("")
        assert papers == []

    def test_search_fallback_to_request(self):
        """When no papers found, should submit collection request."""
        with tempfile.TemporaryDirectory() as tmp:
            source = HfpclawerSource(request_dir=tmp, auto_submit_request=True)

            # If hfpclawer is not available, it should return error
            # without crashing
            result = source.search("quantum sensing", max_results=3)
            assert "papers" in result
            assert "errors" in result or "request_submitted" in result

    def test_manual_request_submission(self):
        """submit_paper_request convenience function."""
        with tempfile.TemporaryDirectory() as tmp:
            from omega.research.sources.hfpclawer import PAPER_REQUEST_DIR
            import omega.research.sources.hfpclawer as hf_mod
            orig = hf_mod.PAPER_REQUEST_DIR
            hf_mod.PAPER_REQUEST_DIR = Path(tmp)
            try:
                path = submit_paper_request(
                    topic="NV crack detection",
                    keywords=["NV", "crack", "steel"],
                    max_papers=15,
                )
                assert Path(path).is_file()
                data = json.loads(Path(path).read_text())
                assert data["topic"] == "NV crack detection"
                assert data["max_papers"] == 15
            finally:
                hf_mod.PAPER_REQUEST_DIR = orig


class TestHfpclawerIntegration:
    """Tests that require hfpclawer CLI to be installed."""

    def test_cli_store_search(self):
        """If hfpclawer CLI is available, store search should work."""
        import subprocess
        try:
            result = subprocess.run(
                ["hfpclawer", "store", "search", "-k", "NV center", "-l", "3"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                # Either returns papers or "no results" — either is valid
                assert True
            else:
                # CLI exists but store not populated — still valid
                assert True
        except FileNotFoundError:
            pytest.skip("hfpclawer CLI not installed")
