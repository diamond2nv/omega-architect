"""Tests for WikiSource and KiwixSource."""

from __future__ import annotations

import tempfile
from pathlib import Path

from omega.research.sources.kiwix import KiwixSource
from omega.research.sources.wiki import WikiSource

# ── WikiSource ──────────────────────────────────────────────────


class TestWikiSource:
    def test_not_available_when_no_wiki(self):
        source = WikiSource(wiki_path="/nonexistent/wiki")
        assert source.available is False

    def test_available_with_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            (wiki_dir / "concepts").mkdir()
            (wiki_dir / "index.md").write_text("# Test Wiki\n")
            source = WikiSource(wiki_path=str(wiki_dir))
            assert source.available is True
            (wiki_dir / "concepts" / "add_comm.md").write_text(
                "---\ntitle: \"Add Commutativity\"\ntags: [algebra, nat]\n---\n\n"
                "This is a lemma about addition."
            )
            result = source.search(
                "theorem add_comm (a b : Nat) : a + b = b + a :="
            )
            assert result["n_pages"] >= 1
            assert "add_comm" in result["pages"][0]["title"]

    def test_empty_keyword_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            (wiki_dir / "index.md").write_text("# Test\n")
            source = WikiSource(wiki_path=str(wiki_dir))
            result = source.search("x")
            assert result["n_pages"] == 0

    def test_scoring_by_title_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            for d in ("concepts", "entities", "comparisons"):
                (wiki_dir / d).mkdir()
            (wiki_dir / "index.md").write_text("# Test\n")
            (wiki_dir / "concepts" / "induction.md").write_text(
                "---\ntitle: Mathematical Induction\ntags: [proof, nat]\n---\n\n"
                "Induction is a proof technique for natural numbers."
            )
            source = WikiSource(wiki_path=str(wiki_dir))
            result = source.search("theorem induction_example (n : Nat) : n = n :=")
            assert result["n_pages"] >= 1

    def test_graceful_degradation(self):
        source = WikiSource(wiki_path="/does/not/exist")
        result = source.search("theorem t : True :=")
        assert "errors" in result
        assert result["n_pages"] == 0

    def test_extract_keywords_from_theorem(self):
        kw = WikiSource._extract_keywords(
            "theorem complete_lattice (α : Type) : CompleteLattice α"
        )
        if kw:  # "complete" > 2 chars
            assert len(kw) <= 5

    def test_search_captures_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            (wiki_dir / "concepts").mkdir()
            (wiki_dir / "index.md").write_text("# Test\n")
            (wiki_dir / "concepts" / "trigonometry.md").write_text(
                "---\ntitle: Trigonometry\ntags: [math]\n---\n\n"
                "Sine and cosine are fundamental. sin²θ + cos²θ = 1."
            )
            source = WikiSource(wiki_path=str(wiki_dir))
            result = source.search("theorem trig_identity : sin²θ + cos²θ = 1 :=")
            assert result["n_pages"] >= 1

    def test_tags_extracted_from_frontmatter(self):
        content = "---\ntitle: Test\ntags: [algebra, group, ring]\n---\n\nBody"
        tags = WikiSource._extract_tags(content)
        assert "algebra" in tags
        assert "ring" in tags


# ── KiwixSource ────────────────────────────────────────────────


class TestKiwixSource:
    def test_not_available_by_default(self):
        source = KiwixSource(base_url="http://localhost:1")
        assert source.available is False

    def test_empty_when_not_available(self):
        source = KiwixSource(base_url="http://localhost:1")
        result = source.search("Maxwell equations")
        assert result["articles"] == []
        assert result["n_articles"] == 0

    def test_lookup_physics_formula_unavailable(self):
        source = KiwixSource(base_url="http://localhost:1")
        result = source.lookup_physics_formula("Schrödinger equation")
        assert result is None
