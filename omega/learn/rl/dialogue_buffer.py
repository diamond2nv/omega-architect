"""
DialogueBuffer — Off-policy replay buffer for LUFFY mixed-policy training.

Wraps DialogueCache with RL-specific sampling:
  - Prioritized replay (by reward / compile success)
  - Domain filtering
  - GRPO-format conversion (theorem + expert code + metadata)
  - LUFFY-compatible sampling interface

Usage:
    from omega.learn.rl.dialogue_buffer import DialogueBuffer
    buffer = DialogueBuffer()
    batch = buffer.sample_for_grpo(n=4, domain="algebra")
    # batch = [(theorem, expert_code, metadata), ...]
    # Then feed into LUFFY trainer's train_step(off_policy_theorems, off_policy_codes)
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("omega.learn.rl.dialogue_buffer")

# ── Domain patterns for automatic classification ──
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "algebra": ["ring", "field", "group", "monoid", "semiring", "galois",
                "polynomial", "matrix", "determinant", "vector_space",
                "module", "ideal", "homomorphism", "comm_ring", "algebra"],
    "number_theory": ["prime", "gcd", "lcm", "divisible", "modulo",
                      "congruent", "factorization", "euler", "fermat",
                      "numbertheory", "int", "ℕ", "nat "],
    "induction": ["induction", "inductive", "nat.rec", "recursive"],
    "calculus": ["derivative", "integral", "limit", "continuous",
                 "differentiable", "analysis", "calculus", "real"],
    "geometry": ["triangle", "circle", "angle", "euclidean", "geometry",
                 "vector", "plane", "point", "line"],
    "logic": ["implies", "iff", "forall", "exists",
              "proposition", "contraposition", "tautology",
              "conjunction", "disjunction", "negation",
              "antecedent", "consequent"],
    "combinatorics": ["finset", "finset.card", "combination", "permutation",
                      "binomial", "factorial", "counting"],
    "probability": ["probability", "expectation", "variance", "random",
                    "distribution", "measure"],
}

# ── Domain weights for multi-domain training ──
# Rarer domains get higher sampling weight
DEFAULT_DOMAIN_WEIGHTS: dict[str, float] = {
    "algebra": 0.15,
    "number_theory": 0.15,
    "induction": 0.20,
    "calculus": 0.10,
    "geometry": 0.10,
    "logic": 0.15,
    "combinatorics": 0.10,
    "probability": 0.05,
}


@dataclass
class BufferEntry:
    """A single off-policy trajectory entry in the replay buffer."""

    theorem_name: str
    theorem_header: str
    proof_code: str
    reward: float = 1.0        # 1.0 = compile success, lower = partial
    domain: str = "unknown"
    timestamp: str = ""
    model: str = "unknown"
    priority: float = 1.0      # for prioritized replay

    def to_grpo_pair(self) -> tuple[str, str]:
        """Convert to (theorem, expert_code) pair for GRPO loss computation."""
        return self.theorem_header, self.proof_code

    @classmethod
    def from_cache_entry(cls, entry: dict) -> BufferEntry:
        """Create from DialogueCache JSONL entry."""
        meta = entry.get("metadata", {})
        code = entry.get("proof_code", "")
        domain = classify_domain(entry.get("theorem_name", ""), entry.get("theorem_header", ""))
        return cls(
            theorem_name=entry.get("theorem_name", ""),
            theorem_header=entry.get("theorem_header", ""),
            proof_code=code,
            reward=1.0 if code and "sorry" not in code else 0.0,
            domain=domain,
            timestamp=meta.get("timestamp", ""),
            model=meta.get("model", "unknown"),
            priority=1.0,
        )


# ── Domain classification ──

def classify_domain(theorem_name: str, theorem_header: str = "") -> str:
    """Classify a theorem into a domain based on keywords.

    Lowercases and searches both the theorem name and header for domain keywords.
    Returns the best-matching domain or 'unknown'.
    """
    text = (theorem_name + " " + theorem_header).lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in text)
        if score > 0:
            scores[domain] = score
    if scores:
        return max(scores, key=scores.get)
    return "unknown"


# ── DialogueBuffer ──

class DialogueBuffer:
    """Off-policy replay buffer wrapping DialogueCache.

    Provides:
    - Prioritized sampling for GRPO training
    - Domain filtering / stratified sampling
    - Automatic domain classification
    - LUFFY-compatible output format
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        max_size: int = 10_000,
        domain_weights: dict[str, float] | None = None,
    ):
        # Default cache dir: same as DialogueCache
        self.cache_dir = Path(cache_dir) if cache_dir else (
            Path.home() / ".cache" / "omega" / "proof_dialogues"
        )
        self.jsonl_path = Path(self.cache_dir) / "proofs.jsonl"
        self.max_size = max_size
        self.domain_weights = domain_weights or DEFAULT_DOMAIN_WEIGHTS.copy()

        # In-memory buffer
        self._entries: list[BufferEntry] = []
        self._domain_index: dict[str, list[int]] = defaultdict(list)  # domain → entry indices

        logger.info("DialogueBuffer: %s (max=%d)", self.jsonl_path, max_size)

    def load(self, force: bool = False) -> int:
        """Load all entries from the JSONL cache into memory.

        Args:
            force: Reload even if already loaded.

        Returns:
            Number of entries loaded.
        """
        if self._entries and not force:
            return len(self._entries)

        self._entries = []
        self._domain_index.clear()

        self.jsonl_path = Path(self.jsonl_path)

        if not self.jsonl_path.exists():
            logger.warning("Cache file not found: %s", self.jsonl_path)
            return 0

        with open(self.jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = BufferEntry.from_cache_entry(data)
                    self._entries.append(entry)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning("Skipping malformed entry: %s", e)

        # Build domain index
        for idx, entry in enumerate(self._entries):
            self._domain_index[entry.domain].append(idx)

        # Apply max_size truncation
        if len(self._entries) > self.max_size:
            self._entries = self._entries[-self.max_size:]
            self._rebuild_index()

        logger.info("Loaded %d entries (%d domains)", len(self._entries), len(self._domain_index))
        return len(self._entries)

    def _rebuild_index(self):
        """Rebuild domain index after truncation."""
        self._domain_index.clear()
        for idx, entry in enumerate(self._entries):
            self._domain_index[entry.domain].append(idx)

    # ── Sampling ──

    def sample_for_grpo(
        self,
        n: int = 4,
        domain: str | None = None,
        strategy: str = "uniform",
        temperature: float = 1.0,
    ) -> list[tuple[str, str, dict]]:
        """Sample n entries for GRPO off-policy loss computation.

        Args:
            n: Number of entries to sample.
            domain: Optional domain filter (e.g., 'algebra', 'induction').
            strategy: 'uniform' (equal weight), 'prioritized' (by reward),
                     'stratified' (by domain_weights).
            temperature: Softmax temperature for prioritized sampling.

        Returns:
            List of (theorem_header, proof_code, metadata) tuples.
        """
        if not self._entries:
            self.load()

        if not self._entries:
            return []

        # Filter by domain
        if domain and domain in self._domain_index:
            indices = self._domain_index[domain]
        elif domain:
            indices = [i for i, e in enumerate(self._entries) if e.domain == domain]
        else:
            indices = list(range(len(self._entries)))

        if not indices:
            logger.warning("No entries for domain '%s'", domain or "any")
            return []

        # Sampling strategy
        if strategy == "uniform":
            selected = random.sample(indices, min(n, len(indices)))
        elif strategy == "prioritized":
            selected = self._sample_prioritized(indices, n, temperature)
        elif strategy == "stratified":
            selected = self._sample_stratified(n)
        else:
            selected = random.sample(indices, min(n, len(indices)))

        return [
            (
                self._entries[i].theorem_header,
                self._entries[i].proof_code,
                {
                    "name": self._entries[i].theorem_name,
                    "domain": self._entries[i].domain,
                    "reward": self._entries[i].reward,
                },
            )
            for i in selected
        ]

    def _sample_prioritized(
        self, indices: list[int], n: int, temperature: float = 1.0
    ) -> list[int]:
        """Sample with priority weighting (higher reward = higher probability)."""
        if len(indices) <= n:
            return indices

        priorities = []
        for i in indices:
            p = self._entries[i].priority ** (1.0 / max(temperature, 0.01))
            priorities.append(p)

        total = sum(priorities)
        weights = [p / total for p in priorities]

        return random.choices(indices, weights=weights, k=n)

    def _sample_stratified(self, n: int) -> list[int]:
        """Stratified sampling by domain weight."""
        if n <= 0:
            return []

        # Allocate slots per domain
        selected = []
        for domain, weight in self.domain_weights.items():
            domain_n = max(1, int(n * weight))
            domain_indices = self._domain_index.get(domain, [])
            if domain_indices:
                k = min(domain_n, len(domain_indices))
                selected.extend(random.sample(domain_indices, k))

        # Fill remaining slots from any domain
        remaining = n - len(selected)
        if remaining > 0:
            all_indices = [i for i in range(len(self._entries)) if i not in selected]
            if all_indices:
                selected.extend(random.sample(all_indices, min(remaining, len(all_indices))))

        return selected[:n]

    # ── Domain statistics ──

    def domain_stats(self) -> dict[str, int]:
        """Return count of entries per domain."""
        return {domain: len(indices) for domain, indices in self._domain_index.items()}

    def summary(self) -> str:
        """Return a human-readable summary."""
        if not self._entries:
            self.load()
        stats = self.domain_stats()
        lines = [
            f"DialogueBuffer: {len(self._entries)} entries",
            f"  Domains: {len(stats)}",
        ]
        for domain, count in sorted(stats.items(), key=lambda x: -x[1]):
            lines.append(f"    {domain}: {count}")
        return "\n".join(lines)

    # ── Add entry ──

    def add_entry(self, entry: BufferEntry):
        """Add a single entry to the buffer and append to cache file."""
        self._entries.append(entry)
        idx = len(self._entries) - 1
        self._domain_index[entry.domain].append(idx)

        # Append to JSONL
        try:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps({
                    "theorem_name": entry.theorem_name,
                    "theorem_header": entry.theorem_header,
                    "proof_code": entry.proof_code,
                    "metadata": {
                        "model": entry.model,
                        "timestamp": entry.timestamp or datetime.utcnow().isoformat(),
                    },
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Failed to append entry: %s", e)

    def add_entries(self, entries: list[BufferEntry]):
        """Add multiple entries."""
        for entry in entries:
            self.add_entry(entry)

    # ── Priority updates ──

    def update_priorities(self, indices: list[int], priorities: list[float]):
        """Update sampling priorities after evaluation."""
        for idx, priority in zip(indices, priorities):
            if 0 <= idx < len(self._entries):
                self._entries[idx].priority = priority

    # ── Clear / reset ──

    def clear(self):
        """Clear in-memory buffer (does NOT delete cache file)."""
        self._entries.clear()
        self._domain_index.clear()
