"""Tests for DialogueBuffer — off-policy replay buffer."""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Direct import: bypass omega.__init__ chain
_buffer_path = os.path.join(
    os.path.dirname(__file__), "..", "omega", "learn", "rl", "dialogue_buffer.py"
)
_modname = "omega.learn.rl.dialogue_buffer"
spec = importlib.util.spec_from_file_location(_modname, _buffer_path)
db_mod = importlib.util.module_from_spec(spec)
sys.modules[_modname] = db_mod
spec.loader.exec_module(db_mod)

DialogueBuffer = db_mod.DialogueBuffer
BufferEntry = db_mod.BufferEntry
classify_domain = db_mod.classify_domain
DOMAIN_KEYWORDS = db_mod.DOMAIN_KEYWORDS


# ── Fixtures ──

@pytest.fixture
def sample_entries() -> list[BufferEntry]:
    return [
        BufferEntry("aime_1983_p1", "theorem aime...", "by nlinarith", domain="algebra"),
        BufferEntry("imo_1959_p1", "theorem imo...", "by induction", domain="induction"),
        BufferEntry("amc12_2001_p5", "theorem amc...", "calc\n  ...", domain="number_theory"),
        BufferEntry("prime_infinite", "theorem primes...", "by omega", domain="number_theory"),
        BufferEntry("triangle_area", "theorem area...", "by nlinarith", domain="geometry"),
        BufferEntry("modus_ponens", "theorem mp...", "by exact h", domain="logic"),
        BufferEntry("sum_of_squares", "theorem sos...", "by omega", domain="algebra"),
        BufferEntry("finset_card_union", "theorem card...", "simp", domain="combinatorics"),
    ]


@pytest.fixture
def dummy_cache_file(sample_entries) -> str:
    """Create a temporary JSONL cache file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for entry in sample_entries:
            f.write(json.dumps({
                "theorem_name": entry.theorem_name,
                "theorem_header": entry.theorem_header,
                "proof_code": entry.proof_code,
                "metadata": {
                    "model": "deepseek-v4-pro",
                    "timestamp": "2026-06-01T00:00:00",
                },
            }) + "\n")
        return f.name


@pytest.fixture
def buffer(dummy_cache_file) -> DialogueBuffer:
    buf = DialogueBuffer(
        cache_dir=Path(dummy_cache_file).parent,
        max_size=100,
    )
    buf.jsonl_path = dummy_cache_file
    return buf


# ── Tests ──

class TestClassifyDomain:
    """Domain classification."""

    def test_algebra(self):
        assert classify_domain("ring_theorem", "theorem ring...") == "algebra"

    def test_induction(self):
        assert classify_domain("nat_induction", "") == "induction"

    def test_number_theory(self):
        assert classify_domain("prime_infinite", "") == "number_theory"

    def test_geometry(self):
        assert classify_domain("triangle_area", "theorem triangle...") == "geometry"

    def test_logic(self):
        assert classify_domain("implies_tautology", "h : A → B") == "logic"

    def test_combinatorics(self):
        assert classify_domain("finset_card", "") == "combinatorics"

    def test_unknown_fallback(self):
        assert classify_domain("xyz_abc", "completely unrelated text here") == "unknown"

    def test_case_insensitive(self):
        assert classify_domain("GroupTheory", "") == "algebra"


class TestDialogueBuffer:
    """DialogueBuffer operations."""

    def test_load_empty(self):
        """Loading from non-existent file returns 0."""
        buf = DialogueBuffer(cache_dir="/tmp/nonexistent_cache_xyz")
        n = buf.load()
        assert n == 0

    def test_load(self, buffer):
        assert buffer.load() == 8

    def test_load_idempotent(self, buffer):
        """Loading twice doesn't double entries."""
        buffer.load()
        buffer.load()
        assert len(buffer._entries) == 8

    def test_load_force(self, buffer):
        buffer.load()
        # Add entry → file now has 9
        buffer.add_entry(BufferEntry("new", "new theorem", "by simp"))
        buffer.load(force=True)  # reload from file (now 9)
        assert len(buffer._entries) == 9  # original + new entry

    def test_domain_stats(self, buffer):
        buffer.load()
        stats = buffer.domain_stats()
        # Domains classified from theorem names in sample fixtures
        assert stats.get("number_theory", 0) >= 1  # "prime_infinite"
        assert stats.get("geometry", 0) >= 1       # "triangle_area"
        assert "unknown" in stats                   # other theroems

    def test_summary(self, buffer):
        buffer.load()
        s = buffer.summary()
        assert "8 entries" in s

    # ── Sampling ──

    def test_sample_uniform(self, buffer):
        buffer.load()
        batch = buffer.sample_for_grpo(n=4, strategy="uniform")
        assert len(batch) == 4
        for thm, code, meta in batch:
            assert isinstance(thm, str)
            assert isinstance(code, str)
            assert "domain" in meta

    def test_sample_more_than_available(self, buffer):
        buffer.load()
        batch = buffer.sample_for_grpo(n=100, strategy="uniform")
        assert len(batch) == 8  # capped at available

    def test_sample_empty_buffer(self):
        buf = DialogueBuffer(cache_dir="/tmp/nonexistent_xyz")
        batch = buf.sample_for_grpo(n=4)
        assert batch == []

    def test_sample_domain_filter(self, buffer):
        buffer.load()
        batch = buffer.sample_for_grpo(n=10, domain="number_theory")
        assert len(batch) >= 1
        for _, _, meta in batch:
            assert meta["domain"] == "number_theory"

    def test_sample_prioritized(self, buffer):
        buffer.load()
        batch = buffer.sample_for_grpo(n=4, strategy="prioritized")
        assert len(batch) == 4

    def test_sample_stratified(self, buffer):
        buffer.load()
        batch = buffer.sample_for_grpo(n=10, strategy="stratified")
        assert len(batch) == 8  # capped

    # ── Add entry ──

    def test_add_entry(self, buffer):
        buffer.load()
        entry = BufferEntry("new_theorem", "theorem new", "by omega", domain="logic")
        buffer.add_entry(entry)
        assert len(buffer._entries) == 9
        assert "logic" in buffer._domain_index

    def test_add_entry_persists(self, buffer, dummy_cache_file):
        """Entry should be appended to cache file."""
        buffer.load()
        entry = BufferEntry("persistent", "thm", "by simp", domain="algebra")
        buffer.add_entry(entry)

        # Re-read
        with open(dummy_cache_file) as f:
            lines = f.readlines()
        assert len(lines) == 9
        assert "persistent" in lines[-1]

    def test_add_entries(self, buffer):
        buffer.load()
        entries = [
            BufferEntry("a", "ta", "ca", domain="logic"),
            BufferEntry("b", "tb", "cb", domain="algebra"),
        ]
        buffer.add_entries(entries)
        assert len(buffer._entries) == 10

    # ── Priority updates ──

    def test_update_priorities(self, buffer):
        buffer.load()
        buffer.update_priorities([0, 1], [5.0, 0.1])
        assert buffer._entries[0].priority == 5.0
        assert buffer._entries[1].priority == 0.1

    def test_update_priorities_out_of_range(self, buffer):
        buffer.load()
        buffer.update_priorities([999], [5.0])  # no crash

    # ── Clear ──

    def test_clear(self, buffer):
        buffer.load()
        assert len(buffer._entries) == 8
        buffer.clear()
        assert len(buffer._entries) == 0

    def test_clear_preserves_file(self, buffer, dummy_cache_file):
        """Clear only clears in-memory, not the cache file."""
        buffer.load()
        buffer.clear()
        assert os.path.isfile(dummy_cache_file)
        assert os.path.getsize(dummy_cache_file) > 0

    # ── Max size ──

    def test_max_size_truncation(self, sample_entries):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for entry in sample_entries:
                f.write(json.dumps({
                    "theorem_name": entry.theorem_name,
                    "theorem_header": entry.theorem_header,
                    "proof_code": entry.proof_code,
                    "metadata": {},
                }) + "\n")

        buf = DialogueBuffer(cache_dir=Path(f.name).parent, max_size=3)
        buf.jsonl_path = f.name
        buf.load()
        assert len(buf._entries) == 3


class TestBufferEntry:
    """BufferEntry conversion and utilities."""

    def test_from_cache_entry(self):
        data = {
            "theorem_name": "test_thm",
            "theorem_header": "theorem t : 1+1=2 :=",
            "proof_code": "by omega",
            "metadata": {
                "model": "deepseek-v4-pro",
                "timestamp": "2026-06-01T00:00:00",
            },
        }
        entry = BufferEntry.from_cache_entry(data)
        assert entry.theorem_name == "test_thm"
        assert entry.reward == 1.0  # no "sorry" in code
        assert entry.domain == "unknown"  # no domain keywords

    def test_from_cache_entry_with_sorry(self):
        data = {
            "theorem_name": "test",
            "theorem_header": "thm",
            "proof_code": "by sorry",
            "metadata": {},
        }
        entry = BufferEntry.from_cache_entry(data)
        assert entry.reward == 0.0  # contains "sorry"

    def test_to_grpo_pair(self):
        entry = BufferEntry("n", "theorem t : True :=", "trivial", domain="logic")
        thm, code = entry.to_grpo_pair()
        assert thm == "theorem t : True :="
        assert code == "trivial"
