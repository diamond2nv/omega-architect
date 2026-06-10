"""Successful proof dialogue cache — saves agent theorem-proving conversations for future fine-tuning or reference.

Each successful proof is appended to a JSONL file. Format compatible with both:
1. Supervised fine-tuning (SFT): problem → solution pairs
2. Multi-turn conversation: full agent dialogue for reinforcement learning or distillation

Usage:
    from omega.loop.dialogue_cache import DialogueCache
    cache = DialogueCache()
    cache.save(inner_loop_result, theorem_name)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.loop.dialogue_cache")

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "omega" / "proof_dialogues"


@dataclass
class DialogueEntry:
    """A single successful proof dialogue entry.
    
    Fields designed for downstream use:
    - SFT: use `problem` + `proof_code` or `messages` (assistant turn only)
    - Multi-turn: use `messages` (full conversation with system/user/assistant/tool)
    - Analysis: use `metadata` fields
    """
    theorem_name: str
    theorem_header: str
    proof_code: str
    messages: list[dict]
    rounds: int
    searches: int
    compiles: int
    sub_lemmas: list[str]
    cost_usd: float
    elapsed_ms: int
    adaptive_strategy_used: bool
    aesop_fallback_used: bool
    timestamp: str
    model: str = "deepseek-v4-pro"
    
    def to_dict(self) -> dict:
        return {
            "theorem_name": self.theorem_name,
            "theorem_header": self.theorem_header,
            "proof_code": self.proof_code,
            "messages": self.messages,
            "metadata": {
                "rounds": self.rounds,
                "searches": self.searches,
                "compiles": self.compiles,
                "sub_lemmas": self.sub_lemmas,
                "cost_usd": self.cost_usd,
                "elapsed_ms": self.elapsed_ms,
                "adaptive_strategy_used": self.adaptive_strategy_used,
                "aesop_fallback_used": self.aesop_fallback_used,
                "timestamp": self.timestamp,
                "model": self.model,
            },
        }


class DialogueCache:
    """Persistent cache of successful proof dialogues."""
    
    def __init__(self, cache_dir: str | Path = _DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self.cache_dir / "proofs.jsonl"
        logger.info("Dialogue cache: %s", self._jsonl_path)
    
    def save(self, result, theorem_name: str) -> bool:
        """Save a successful InnerLoopResult to the cache.
        
        Returns True if saved, False if skipped (not a success).
        """
        if not result.success:
            return False
        
        # Count searches and compiles from history
        searches = sum(
            1 for m in result.history
            if m.get("role") == "assistant"
            and m.get("tool_calls") and len(m["tool_calls"]) > 0
        )
        compiles = sum(
            1 for m in result.history
            if m.get("role") == "user"
            and "Compilation failed" in (m.get("content", "") or "")
        )
        
        # Strip full tool_call details to keep cache lean for SFT
        messages_clean = []
        for m in result.history:
            entry = {"role": m["role"]}
            if m.get("content"):
                entry["content"] = m["content"]
            if m.get("reasoning_content"):
                entry["reasoning_content"] = m["reasoning_content"]
            messages_clean.append(entry)
        
        entry = DialogueEntry(
            theorem_name=theorem_name,
            theorem_header=result.theorem_header,
            proof_code=result.code or "",
            messages=messages_clean,
            rounds=result.rounds,
            searches=searches,
            compiles=compiles,
            sub_lemmas=list(result.sub_lemmas),
            cost_usd=result.budget_used_cost,
            elapsed_ms=result.total_elapsed_ms,
            adaptive_strategy_used=result.adaptive_strategy_used,
            aesop_fallback_used=result.aesop_fallback_used,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            logger.info(
                "Cached proof dialogue: %s (%d rounds, $%.4f)",
                theorem_name, result.rounds, result.budget_used_cost,
            )
            return True
        except Exception as e:
            logger.warning("Failed to cache proof dialogue: %s", e)
            return False
    
    def list_proofs(self) -> list[dict]:
        """List all cached proofs (metadata only, no messages)."""
        proofs = []
        if not self._jsonl_path.exists():
            return proofs
        with open(self._jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    proofs.append({
                        "theorem_name": data["theorem_name"],
                        **data["metadata"],
                    })
        return proofs
    
    def count(self) -> int:
        """Number of cached proofs."""
        if not self._jsonl_path.exists():
            return 0
        count = 0
        with open(self._jsonl_path) as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
    
    def export_sft(self) -> list[dict]:
        """Export as SFT pairs: {problem, solution} format.
        
        Each entry:
        - problem: the theorem header
        - solution: the final proof code
        - system_prompt: the system prompt used
        """
        pairs = []
        if not self._jsonl_path.exists():
            return pairs
        with open(self._jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    pairs.append({
                        "problem": data["theorem_header"],
                        "solution": data["proof_code"],
                        "name": data["theorem_name"],
                    })
        return pairs
