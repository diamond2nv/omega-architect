"""DEC — line-level edit distance utility for EA-GRPO.

Levenshtein distance on line sequences, normalized by source line count.
Used in:
  - compute_ea_grpo_reward()  — reward function
  - FixPMetric                — evaluation metric (fixp@k)

Reference: QiMeng-PRepair (arXiv 2604.05963) §2.2 Eq.3
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def levenshtein_lines(source: str, target: str) -> float:
    """Line-level Levenshtein distance, normalized by source line count.

    Args:
        source: Original (buggy) code.
        target: Generated (fixed) code.

    Returns:
        Normalized edit distance DEC ∈ [0, 1].
        DEC = D(X, Y) / |X| where D is line-level Levenshtein distance.

    Reference:
        QiMeng-PRepair §2.2: "Edit Cost DEC, which is based on the
        Levenshtein distance D(X, Y). We define DEC = D(X, Y) / |X|."
    """
    src_lines = source.splitlines(keepends=False)
    tgt_lines = target.splitlines(keepends=False)

    n, m = len(src_lines), len(tgt_lines)

    # Standard DP for Levenshtein distance
    # Use space-optimized version: only keep two rows
    prev = list(range(m + 1))
    curr = [0] * (m + 1)

    for i in range(1, n + 1):
        curr[0] = i
        for j in range(1, m + 1):
            cost = 0 if src_lines[i - 1] == tgt_lines[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,          # deletion
                curr[j - 1] + 1,      # insertion
                prev[j - 1] + cost,   # substitution
            )
        prev, curr = curr, prev

    dist = prev[m]

    # Normalize by source line count
    if n == 0:
        return 0.0  # empty source → no edit cost
    return dist / n


def compute_dec(source: str, target: str) -> float:
    """Alias for levenshtein_lines — shorter name for reward computation."""
    return levenshtein_lines(source, target)


# ── fixp@k metric ──────────────────────────────────────────────


@dataclass
class FixPCandidate:
    """A single candidate with its fixp score.

    Attributes:
        code: Generated Lean code.
        correct: Whether the proof compiles (passes T2).
        edit_cost: DEC(source, candidate).
        dec_ratio: DEC(candidate) / DEC(golden_fix) — how close to ideal.
        fixp_score: 1 if correct AND dec_ratio ≤ p, else 0.
    """
    code: str
    correct: bool
    edit_cost: float = 0.0
    dec_ratio: float = 1.0
    fixp_score: float = 0.0


@dataclass
class FixPReport:
    """fixp@k evaluation report for a single theorem.

    Reference: QiMeng-PRepair §2.2.
    """
    theorem_name: str
    candidates: list[FixPCandidate] = field(default_factory=list)
    k: int = 1
    p: float = 1.0

    @property
    def pass_at_k(self) -> float:
        """Standard pass@k: any correct candidate in top-k."""
        top = self.candidates[:self.k]
        return 1.0 if any(c.correct for c in top) else 0.0

    @property
    def fixp_at_k(self) -> float:
        """fixp@k: correct AND dec_ratio ≤ p in top-k."""
        top = self.candidates[:self.k]
        return 1.0 if any(c.fixp_score > 0 for c in top) else 0.0

    def summary(self) -> str:
        n = len(self.candidates[:self.k])
        correct_n = sum(1 for c in self.candidates[:self.k] if c.correct)
        fixp_n = sum(1 for c in self.candidates[:self.k] if c.fixp_score > 0)
        avg_dec = sum(c.edit_cost for c in self.candidates[:self.k]) / max(n, 1)
        return (
            f"[{self.theorem_name}] "
            f"pass@{self.k}={correct_n}/{n}  "
            f"fix{self.p}@{self.k}={fixp_n}/{n}  "
            f"avg_DEC={avg_dec:.3f}"
        )


def compute_fixp_candidates(
    source_code: str,
    candidates: list[str],
    compiler_results: list[bool],
    golden_fix: str | None = None,
    k: int = 1,
    p: float = 1.0,
) -> FixPReport:
    """Compute fixp@k for a set of candidate proofs.

    Args:
        source_code: The (buggy) source code.
        candidates: Generated candidate proofs.
        compiler_results: Whether each candidate compiled.
        golden_fix: Reference correct proof (optional). If None, uses minimum
            edit cost among candidates as surrogate.
        k: Number of candidates to consider (default 1).
        p: DEC ratio threshold (default 1.0). A candidate is "precise" if
            DEC(candidate) / DEC(golden) ≤ p.

    Returns:
        FixPReport with per-candidate scores.
    """
    if golden_fix:
        dec_golden = compute_dec(source_code, golden_fix)
    else:
        # Use minimum DEC among correct candidates as surrogate
        correct_decs = [
            compute_dec(source_code, c)
            for c, ok in zip(candidates, compiler_results)
            if ok
        ]
        dec_golden = min(correct_decs) if correct_decs else 1.0

    if dec_golden < 1e-10:
        dec_golden = 1.0  # avoid division by zero

    fixp_list: list[FixPCandidate] = []
    for code, correct in zip(candidates, compiler_results):
        dec = compute_dec(source_code, code)
        ratio = dec / dec_golden
        fixp_score = 1.0 if (correct and ratio <= p) else 0.0
        fixp_list.append(FixPCandidate(
            code=code, correct=correct, edit_cost=dec,
            dec_ratio=ratio, fixp_score=fixp_score,
        ))

    return FixPReport(
        theorem_name="",
        candidates=fixp_list,
        k=k,
        p=p,
    )


# ── EA-GRPO reward ─────────────────────────────────────────────


def compute_ea_grpo_reward(
    candidate_codes: list[str],
    source_code: str,
    compile_results: list[bool],
    group_accuracy_threshold: float = 0.5,
    edit_penalty_beta: float = 0.3,
) -> list[float]:
    """Edit-Aware GRPO reward.

    QiMeng-PRepair §2.4 Eq.5-6:

        R_i = { 1 - T(G) * beta * sigmoid(z_i)   if correct
                0                                  if incorrect

        z_i = (DEC_i - mean) / std    (standardized within group)
        T(G) = 1 if Acc_G >= alpha else 0   (dynamic switch)

    Args:
        candidate_codes: Generated proof candidates.
        source_code: The original (buggy) code.
        compile_results: Whether each candidate compiles (correctness).
        group_accuracy_threshold: Alpha — group accuracy threshold for
            penalty activation. Default 0.5 (50% of group must be correct).
        edit_penalty_beta: Beta — penalty strength. Default 0.3.

    Returns:
        List of rewards, one per candidate.
    """
    n = len(candidate_codes)
    if n == 0:
        return []

    # 1. Compute edit costs for all candidates
    decs = [compute_dec(source_code, code) for code in candidate_codes]

    # 2. Group accuracy
    group_accuracy = sum(1 for r in compile_results if r) / n

    # 3. Dynamic penalty switch T(G)
    penalty_active = 1.0 if group_accuracy >= group_accuracy_threshold else 0.0

    # 4. Standardize edit costs within correct candidates
    #    Uses group-level statistics (including incorrect candidates for
    #    normalization — matches paper: "mean and std of edit cost for
    #    correct samples in the group")
    correct_indices = [i for i, ok in enumerate(compile_results) if ok]
    if correct_indices:
        correct_decs = [decs[i] for i in correct_indices]
        mean_dec = sum(correct_decs) / len(correct_decs)
        var_dec = sum((d - mean_dec) ** 2 for d in correct_decs) / len(correct_decs)
        std_dec = math.sqrt(var_dec + 1e-8)  # epsilon for numerical stability
    else:
        mean_dec = 0.0
        std_dec = 1.0

    # 5. Compute rewards
    rewards: list[float] = []
    for i in range(n):
        if not compile_results[i]:
            rewards.append(0.0)
            continue

        # Low variance → no meaningful discrimination, skip penalty
        if std_dec < 1e-3:
            reward = 1.0
        else:
            z = (decs[i] - mean_dec) / std_dec
            penalty = 1.0 / (1.0 + math.exp(-z))
            reward = 1.0 - penalty_active * edit_penalty_beta * penalty
        rewards.append(reward)

    return rewards
