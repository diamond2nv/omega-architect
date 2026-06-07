#!/usr/bin/env python3
"""ModelAllocator — strategic local/remote model routing for Omega.

Determines which model to use for each theorem based on:
1. **Complexity classification** (domain, header structure, expected difficulty)
2. **Local failure count** (escalate to remote after N failures)
3. **Task type** (blueprint/strategy → remote, routine search → local)

This implements the "国产模型联合优势" — use local GPU for fast scanning and
routine proofs, reserve remote DeepSeek-v4-pro for high-value reasoning.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

logger = __import__("logging").getLogger("omega.resource.allocator")


# ── Complexity classification ────────────────────────────────────


class ComplexityClass(enum.Enum):
    """Theorem complexity for model routing."""

    EASY = "easy"
    """simp/trivial, known pattern, simple induction. → local gemma4 (fastest)."""

    MEDIUM = "medium"
    """Requires lemma search, case analysis, moderate reasoning. → local deepseek-r1 or qwen3-coder."""

    HARD = "hard"
    """Requires proof blueprint, multi-step strategy. → remote deepseek-v4-pro (blueprint only)."""

    RESEARCH = "research"
    """Theoretical framing, domain knowledge synthesis. → remote deepseek-v4-pro."""


COMPLEX_DOMAINS: set[str] = {
    "quantum",
    "optics",
    "group_theory",
    "galois",
    "homology",
    "cohomology",
    "category",
    "topos",
    "knot",
    "spinor",
    "lie_algebra",
    "representation",
    "spectral",
}

# Regex patterns that indicate hard theorems
HARD_PATTERNS: list[re.Pattern] = [
    re.compile(r"inverse|convergen|continuou|differentiab|integral"),
    re.compile(r"isomorphi|equivarian|natural.trans|adjunction"),
    re.compile(r"fixed_point|banach|hilbert|sobolev"),
    re.compile(r"spectral|eigenvalue|singular|decomposition"),
    re.compile(r"hamiltoni|lagrangian|noether|symplect"),
]

EASY_PATTERNS: list[re.Pattern] = [
    re.compile(r"add_comm|add_assoc|mul_comm|mul_assoc|add_zero|zero_add"),
    re.compile(r"succ_eq_add_one|add_succ|succ_add"),
    re.compile(r"by\s*simp$|by\s*trivial$|by\s*rfl$"),
]


# ── Model configurations ────────────────────────────────────────


@dataclass
class ModelConfig:
    """Configuration for a model in the allocator."""

    model_id: str
    """Full model_id (e.g. ``ollama/gemma4:26b``, ``deepseek/deepseek-chat``)."""

    tier: str = "local"
    """'local' or 'remote'."""

    max_attempts: int = 5
    """Max attempts before escalating."""

    tok_s: float = 0.0
    """Expected throughput (set during init)."""

    cost_per_1k_tokens: float = 0.0
    """USD per 1k tokens (0 for local)."""

    strengths: list[str] = field(default_factory=lambda: ["general"])
    """Task types this model is good at."""


# ── Allocation result ───────────────────────────────────────────


@dataclass
class Allocation:
    """Result of a model allocation decision."""

    model_id: str
    """Selected model_id."""

    complexity: ComplexityClass
    """Classified complexity."""

    reason: str
    """Why this model was chosen."""

    local_attempts: int = 0
    """How many local attempts before this allocation."""

    blueprint_mode: bool = False
    """If True, use this model only for blueprint/strategy, not full proof."""

    append_only: bool = False
    """If True, use this model only for continuation/append (cheap flash mode)."""

    expected_cost: float = 0.0
    """Estimated cost for this attempt."""


# ── Provenance ──────────────────────────────────────────────────


@dataclass
class ProvenanceRecord:
    """Record of one theorem proving attempt."""

    theorem_header: str
    model_id: str
    complexity: ComplexityClass
    succeeded: bool
    elapsed_s: float
    n_attempts: int
    tokens_consumed: int
    cost_usd: float
    blueprint_used: bool = False
    append_only: bool = False
    error: str = ""
    tier: str = ""

    def __post_init__(self) -> None:
        if not self.tier:
            # Infer tier from model_id
            local_prefixes = ("ollama/", "local/")
            self.tier = (
                "local" if any(self.model_id.startswith(p) for p in local_prefixes) else "remote"
            )


# ── Allocator ───────────────────────────────────────────────────

# Default model tiers — hardcoded for resilience
DEFAULT_LOCAL_MODELS: list[ModelConfig] = [
    ModelConfig("ollama/gemma4:26b", "local", 5, 64.9, 0.0, ["fast", "easy", "routine"]),
    ModelConfig("ollama/deepseek-r1:8b", "local", 5, 31.9, 0.0, ["reasoning", "exploration"]),
    ModelConfig("ollama/qwen3-coder:30b", "local", 5, 27.3, 0.0, ["code", "lemma_search"]),
    ModelConfig("ollama/qwen3.6:latest", "local", 5, 18.7, 0.0, ["deep_reasoning"]),
]

DEFAULT_REMOTE_MODELS: list[ModelConfig] = [
    ModelConfig(
        "deepseek/deepseek-chat", "remote", 3, 50.0, 2.8e-4, ["strategy", "blueprint", "hard"]
    ),
    ModelConfig(
        "deepseek/deepseek-v4-flash", "remote", 10, 80.0, 7.5e-5, ["continuation", "append", "bulk"]
    ),
]


class ModelAllocator:
    """Strategic model router for Omega theorem proving.

    Routes theorems to the right model based on complexity, domain,
    and failure history.  Implements "hard problems → remote DeepSeek-v4-pro
    for blueprint, routine proofs → local GPU for throughput".
    """

    def __init__(
        self,
        local_models: list[ModelConfig] | None = None,
        remote_models: list[ModelConfig] | None = None,
        escalation_threshold: int = 3,
        max_remote_budget_usd: float = 100.00,
        max_flash_budget_usd: float = 50.00,
        max_pro_budget_usd: float = 50.00,
    ) -> None:
        """Strategic model router.

        Budget defaults are DELIBERATELY GENEROUS for demo/onboarding.
        Third-party users evaluate functionality first (can it prove
        the theorem?), cost optimization comes later.

        Actual DeepSeek costs (June 2026, post-April price-cut):
          Flash: ¥1/M in, ¥2/M out  ($0.14/$0.28)
          Pro:   ¥3/M in, ¥6/M out  ($0.42/$0.83)

        Real per-theorem cost (GoedelProver: 32 LLM calls/attempt):
          Easy:   ¥0.2    (DeepSeek Flash)
          Medium: ¥2.4
          Hard:   ¥34
          V.Hard: ¥85

        Demo budgets ($100 total) give 10-500x headroom above real cost.
        Tighten these once the user confirms the system works.
        """
        import copy

        self._local: list[ModelConfig] = (
            copy.deepcopy(local_models)
            if local_models
            else [copy.deepcopy(m) for m in DEFAULT_LOCAL_MODELS]
        )
        self._remote: list[ModelConfig] = (
            copy.deepcopy(remote_models)
            if remote_models
            else [copy.deepcopy(m) for m in DEFAULT_REMOTE_MODELS]
        )
        self._escalation_threshold = escalation_threshold
        self._max_remote_budget = max_remote_budget_usd
        self._max_flash_budget = max_flash_budget_usd
        self._max_pro_budget = max_pro_budget_usd

        # Provenance tracking
        self._provenance: list[ProvenanceRecord] = []

        # Per-theorem local attempt counter
        self._local_attempts: dict[str, int] = {}

        # Remote budget spent (by tier)
        self._remote_spent: float = 0.0
        self._flash_spent: float = 0.0
        self._pro_spent: float = 0.0

    # ── Classification ────────────────────────────────────────

    def classify(self, theorem_header: str, domain: str = "") -> ComplexityClass:
        """Classify theorem complexity from header text and domain."""
        header_lower = theorem_header.lower()

        # Domain-based: complex domains are HARD
        if domain and domain.lower() in COMPLEX_DOMAINS:
            return ComplexityClass.HARD

        # EASY patterns
        for pat in EASY_PATTERNS:
            if pat.search(header_lower):
                return ComplexityClass.EASY

        # HARD patterns
        for pat in HARD_PATTERNS:
            if pat.search(header_lower):
                return ComplexityClass.HARD

        # Length-based heuristic
        if len(theorem_header) > 200:
            return ComplexityClass.HARD

        return ComplexityClass.MEDIUM

    # ── Model selection ───────────────────────────────────────

    def select_model(
        self,
        theorem_header: str,
        domain: str = "",
        _force_blueprint_only: bool = False,
        _force_append_only: bool = False,
    ) -> Allocation:
        """Select optimal model for a theorem.

        2-tier remote strategy:
        - **Flash** (deepseek-v4-flash): cheap ($0.075/M), fast (80 tok/s).
          Used for **append-only bulk continuation** — take existing partial
          proof and fill in details.  ``append_only=True`` signals the prover
          to pass the existing proof code as context and ask the model to
          continue rather than start from scratch.
        - **Pro** (deepseek/deepseek-chat): expensive ($0.28/M), deep reasoning.
          Used only for **proof blueprint/strategy** for hard theorems after
          3 local failures.  ``blueprint_mode=True`` signals "generate plan,
          not final proof".

        Algorithm:
        1. Classify complexity
        2. If EASY → local gemma4 (fastest)
        3. If MEDIUM → local deepseek-r1; if local exhausted → flash append
        4. If HARD:
           - ≤3 local failures → local qwen3.6
           - Escalated, first remote → **pro** (blueprint)
           - Escalated, continuation → **flash** (append)
        5. If RESEARCH → remote pro (blueprint)
        """
        complexity = self.classify(theorem_header, domain)
        key = theorem_header[:80]

        local_attempts = self._local_attempts.get(key, 0)
        remote_attempts = sum(
            1 for r in self._provenance if r.theorem_header[:80] == key and r.tier == "remote"
        )

        # ── EASY ───────────────────────────────────────────────
        if complexity == ComplexityClass.EASY:
            model = self._local[0]  # gemma4: fastest
            return Allocation(
                model_id=model.model_id,
                complexity=complexity,
                reason=f"EASY: using {model.model_id} ({model.tok_s} tok/s)",
                local_attempts=local_attempts,
                blueprint_mode=False,
                append_only=False,
            )

        # ── MEDIUM ─────────────────────────────────────────────
        if complexity == ComplexityClass.MEDIUM:
            # Try local first (up to escalation threshold)
            if local_attempts < self._escalation_threshold:
                model = self._local[1]  # deepseek-r1
                return Allocation(
                    model_id=model.model_id,
                    complexity=complexity,
                    reason=f"MEDIUM: local attempt {local_attempts + 1}/{self._escalation_threshold} with {model.model_id}",
                    local_attempts=local_attempts,
                    blueprint_mode=False,
                    append_only=False,
                )
            # Escalate to flash for continuation
            if self._remote and self._flash_spent < self._max_flash_budget:
                flash = self._remote[1] if len(self._remote) > 1 else self._remote[0]
                return Allocation(
                    model_id=flash.model_id,
                    complexity=complexity,
                    reason=f"MEDIUM: escalated to flash {flash.model_id} for append (after {local_attempts} local)",
                    local_attempts=local_attempts,
                    blueprint_mode=False,
                    append_only=True,
                )
            # Flash budget exhausted → keep local
            model = self._local[1]
            return Allocation(
                model_id=model.model_id,
                complexity=complexity,
                reason=f"MEDIUM: flash budget exhausted, continuing local with {model.model_id}",
                local_attempts=local_attempts,
                blueprint_mode=False,
                append_only=False,
            )

        # ── RESEARCH ───────────────────────────────────────────
        if complexity == ComplexityClass.RESEARCH:
            if self._remote and self._pro_spent < self._max_pro_budget:
                model = self._remote[0]  # pro
                return Allocation(
                    model_id=model.model_id,
                    complexity=complexity,
                    reason=f"RESEARCH: using pro {model.model_id} for domain synthesis",
                    local_attempts=local_attempts,
                    blueprint_mode=True,
                    append_only=False,
                )
            # Fallback to flash
            if self._remote and self._flash_spent < self._max_flash_budget:
                flash = self._remote[1] if len(self._remote) > 1 else self._remote[0]
                return Allocation(
                    model_id=flash.model_id,
                    complexity=complexity,
                    reason=f"RESEARCH: pro budget exhausted, using flash {flash.model_id}",
                    local_attempts=local_attempts,
                    blueprint_mode=False,
                    append_only=True,
                )
            # All remote exhausted → local
            model = self._local[-1]
            return Allocation(
                model_id=model.model_id,
                complexity=complexity,
                reason=f"RESEARCH: all remote exhausted, using local {model.model_id}",
                local_attempts=local_attempts,
                blueprint_mode=False,
                append_only=False,
            )

        # ── HARD ───────────────────────────────────────────────
        if complexity == ComplexityClass.HARD:
            if local_attempts < self._escalation_threshold and self._local:
                model = self._local[-1]  # qwen3.6
                return Allocation(
                    model_id=model.model_id,
                    complexity=complexity,
                    reason=f"HARD: local attempt {local_attempts + 1}/{self._escalation_threshold} with {model.model_id}",
                    local_attempts=local_attempts,
                    blueprint_mode=False,
                    append_only=False,
                )

            # Escalated to remote — route by attempt type
            # First remote call: pro for blueprint
            if remote_attempts == 0 and self._pro_spent < self._max_pro_budget:
                pro = self._remote[0]
                return Allocation(
                    model_id=pro.model_id,
                    complexity=complexity,
                    reason=f"HARD: blueprint with pro {pro.model_id} (after {local_attempts} local, {remote_attempts} remote)",
                    local_attempts=local_attempts,
                    blueprint_mode=True,
                    append_only=False,
                )

            # Subsequent remote calls: flash for bulk continuation
            if self._flash_spent < self._max_flash_budget:
                flash = self._remote[1] if len(self._remote) > 1 else self._remote[0]
                return Allocation(
                    model_id=flash.model_id,
                    complexity=complexity,
                    reason=f"HARD: append with flash {flash.model_id} (attempt #{remote_attempts + 1})",
                    local_attempts=local_attempts,
                    blueprint_mode=False,
                    append_only=True,
                )

            # All remote exhausted → local fallback
            model = self._local[-1]
            return Allocation(
                model_id=model.model_id,
                complexity=complexity,
                reason=f"HARD: remote budget exhausted, using local {model.model_id}",
                local_attempts=local_attempts,
                blueprint_mode=False,
                append_only=False,
            )

        # ── Fallback ───────────────────────────────────────────
        model = self._local[0]
        return Allocation(
            model_id=model.model_id,
            complexity=complexity,
            reason=f"DEFAULT: using {model.model_id}",
            local_attempts=local_attempts,
            blueprint_mode=False,
            append_only=False,
        )

    def select_blueprint_model(self) -> str:
        """Select the model for proof blueprint generation (always remote if available)."""
        if self._remote and self._pro_spent < self._max_pro_budget:
            return self._remote[0].model_id
        # Fallback: if pro budget exhausted, use flash for blueprint instead
        if self._remote and self._flash_spent < self._max_flash_budget:
            flash = self._remote[1] if len(self._remote) > 1 else self._remote[0]
            return flash.model_id
        return self._local[-1].model_id  # fallback to local deep reasoning

    def select_continuation_model(self) -> str:
        """Select model for bulk continuation/append (always flash if available).

        The continuation model is used to take an existing partial proof and
        generate the next steps — cheap, fast, high token volume.
        """
        if self._remote and self._flash_spent < self._max_flash_budget:
            flash = self._remote[1] if len(self._remote) > 1 else self._remote[0]
            return flash.model_id
        if self._remote and self._pro_spent < self._max_pro_budget:
            return self._remote[0].model_id
        return self._local[0].model_id

    # ── Outcome tracking ──────────────────────────────────────

    def record_outcome(
        self,
        theorem_header: str,
        model_id: str,
        complexity: ComplexityClass,
        succeeded: bool,
        elapsed_s: float = 0.0,
        n_attempts: int = 0,
        tokens_consumed: int = 0,
        cost_usd: float = 0.0,
        blueprint_used: bool = False,
        append_only: bool = False,
        error: str = "",
    ) -> None:
        """Record a theorem proving outcome for provenance and adaptation.

        Args:
            append_only: If True, this attempt used the flash model in
                continuation mode (taking existing partial proof and
                appending steps).  Tracks against the flash budget.
        """
        record = ProvenanceRecord(
            theorem_header=theorem_header,
            model_id=model_id,
            complexity=complexity,
            succeeded=succeeded,
            elapsed_s=elapsed_s,
            n_attempts=n_attempts,
            tokens_consumed=tokens_consumed,
            cost_usd=cost_usd,
            blueprint_used=blueprint_used,
            append_only=append_only,
            error=error,
        )
        self._provenance.append(record)

        # Track per-theorem attempt counters + budget by tier
        key = theorem_header[:80]
        if self._tier(model_id) == "local":
            self._local_attempts[key] = self._local_attempts.get(key, 0) + 1
        else:
            self._remote_spent += cost_usd
            remote_class = self._classify_remote_model(model_id)
            if remote_class == "flash":
                self._flash_spent += cost_usd
            else:
                self._pro_spent += cost_usd

    def _tier(self, model_id: str) -> str:
        """Determine if model_id is local (ollama/ or local/ prefix)."""
        local_prefixes = ("ollama/", "local/")
        return "local" if any(model_id.startswith(p) for p in local_prefixes) else "remote"

    def _classify_remote_model(self, model_id: str) -> str:
        """Classify a remote model as 'flash' or 'pro' based on model ID.

        Uses prefix-based matching to handle various routing patterns:
          - ``deepseek/deepseek-v4-flash``
          - ``openrouter/deepseek/deepseek-v4-flash``
          - ``deepseek/deepseek-chat`` (v4-pro equivalent)
        """
        model_lower = model_id.lower()
        flash_indicators = [
            "flash",
            "deepseek/deepseek-v4-flash",
            "openrouter/deepseek/deepseek-v4-flash",
        ]
        pro_indicators = ["deepseek-chat", "deepseek/deepseek-chat", "deepseek-v4-pro", "pro"]
        for indicator in flash_indicators:
            if indicator in model_lower:
                return "flash"
        for indicator in pro_indicators:
            if indicator in model_lower:
                return "pro"
        # Default: if model_id starts with a known free prefix, it's not remote
        if any(model_id.startswith(p) for p in ("ollama/", "local/")):
            return "local"
        return "pro"  # conservative: unknown remote → treat as pro

    # ── Reporting ─────────────────────────────────────────────

    def provenance_summary(self) -> str:
        """Format provenance as a human-readable table."""
        if not self._provenance:
            return "No provenance records."

        local_ok = sum(1 for r in self._provenance if r.tier == "local" and r.succeeded)
        local_fail = sum(1 for r in self._provenance if r.tier == "local" and not r.succeeded)
        flash_ok = sum(1 for r in self._provenance if r.append_only and r.succeeded)
        flash_fail = sum(1 for r in self._provenance if r.append_only and not r.succeeded)
        pro_ok = sum(
            1 for r in self._provenance if r.tier == "remote" and not r.append_only and r.succeeded
        )
        pro_fail = sum(
            1
            for r in self._provenance
            if r.tier == "remote" and not r.append_only and not r.succeeded
        )
        total_cost = sum(r.cost_usd for r in self._provenance if r.tier == "remote")
        flash_cost = sum(r.cost_usd for r in self._provenance if r.append_only)
        pro_cost = total_cost - flash_cost

        lines = [
            "=" * 60,
            "ModelAllocator Provenance",
            "=" * 60,
            f"Local:  ✅ {local_ok} / ❌ {local_fail}  ({(local_ok / max(local_ok + local_fail, 1)) * 100:.0f}%)",
            f"Pro:    ✅ {pro_ok} / ❌ {pro_fail}  ${pro_cost:.4f}  (blueprint/strategy)",
            f"Flash:  ✅ {flash_ok} / ❌ {flash_fail}  ${flash_cost:.4f}  (append/continuation)",
            f"Total remote cost: ${total_cost:.4f}",
            "",
            "Recent records (last 5):",
        ]
        for r in self._provenance[-5:]:
            icon = "✅" if r.succeeded else "❌"
            if r.append_only:
                tier = "⚡"  # flash
            elif r.tier == "remote":
                tier = "🌟"  # pro
            else:
                tier = "🖥"  # local
            mode = " [append]" if r.append_only else " [blueprint]" if r.blueprint_used else ""
            desc = r.theorem_header[:50]
            lines.append(f"  {icon}{tier} {desc}{mode}  ({r.elapsed_s:.0f}s, {r.n_attempts} att)")
        return "\n".join(lines)

    def reset_local_counters(self) -> None:
        """Reset per-theorem local attempt counters (for a new run)."""
        self._local_attempts.clear()

    def cost_report(self) -> dict[str, Any]:
        """Return cost breakdown with flash/pro split."""
        total = sum(r.cost_usd for r in self._provenance)
        by_model: dict[str, float] = {}
        for r in self._provenance:
            by_model[r.model_id] = by_model.get(r.model_id, 0.0) + r.cost_usd
        return {
            "total_usd": total,
            "by_model": by_model,
            "pro_spent": self._pro_spent,
            "flash_spent": self._flash_spent,
            "pro_remaining": max(self._max_pro_budget - self._pro_spent, 0.0),
            "flash_remaining": max(self._max_flash_budget - self._flash_spent, 0.0),
            "remote_remaining": max(self._max_remote_budget - self._remote_spent, 0.0),
        }

    def _remote_remaining(self) -> float:
        return max(self._max_remote_budget - self._remote_spent, 0.0)
