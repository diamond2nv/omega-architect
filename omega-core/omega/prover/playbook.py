"""Omega Proof Playbook — structured context evolution for self-improving provers.

ACE-inspired: playbook with bullet points, helpful/harmful counters,
incremental delta updates. No full context rewrites → no context collapse.

Usage:

    playbook = Playbook()
    mgr = PlaybookManager(playbook)

    # After each T2 compile:
    mgr.update_from_result(theorem, attempt_code, t2_diagnostics)

    # Before next attempt:
    prompt = mgr.inject_into_prompt(template)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.prover.playbook")

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_MAX_TOKENS = 8_000  # ACE uses 80k; Omega proof contexts are smaller
BULLET_SECTION_SLUGS = {
    "strategies": "str",
    "mistakes": "mis",
    "templates": "fmt",
}


# ── Data structures ──────────────────────────────────────────────────


@dataclass
class PlaybookBullet:
    """A single structured entry in the proof strategy playbook.

    Attributes
    ----------
    bullet_id : str
        Unique identifier with section slug, e.g. ``"str-00001"``.
    section : str
        Semantic section: ``"strategies"``, ``"mistakes"``, or ``"templates"``.
    content : str
        The actual advice, pitfall, or formula template.
    helpful : int
        Number of successful proof attempts where this bullet was applicable.
    harmful : int
        Number of failed attempts where this bullet was applicable.
    created_epoch : int
        Epoch (attempt batch) when this bullet was first created.
    last_used_epoch : int
        Last epoch when this bullet was referenced.
    """

    bullet_id: str
    section: str
    content: str
    helpful: int = 0
    harmful: int = 0
    created_epoch: int = 0
    last_used_epoch: int = 0

    @property
    def score(self) -> float:
        """Net helpfulness score. Negative means this bullet is harmful."""
        return float(self.helpful - self.harmful)

    @property
    def is_harmful(self) -> bool:
        """True when harmful outweighs helpful by 2x or more."""
        return self.harmful > max(self.helpful * 2, 2)

    def render(self) -> str:
        """Render as a single line in the playbook text block."""
        return f"[{self.bullet_id}] helpful={self.helpful} harmful={self.harmful} :: {self.content}"


@dataclass
class Playbook:
    """Structured, evolving context of proof strategies.

    ACE-inspired: playbook grows via incremental delta updates (append /
    update in place) rather than full rewrites, preventing context
    collapse. Pruning / deduplication runs only when token budget is
    exceeded (lazy).
    """

    bullets: list[PlaybookBullet] = field(default_factory=list)
    max_tokens: int = DEFAULT_MAX_TOKENS
    _next_id: int = 1

    # ── Mutation ───────────────────────────────────────────────────

    def add_bullet(
        self,
        section: str,
        content: str,
        epoch: int = 0,
    ) -> PlaybookBullet:
        """Append a new bullet and return it."""
        slug = BULLET_SECTION_SLUGS.get(section, "gen")
        bullet_id = f"{slug}-{self._next_id:05d}"
        self._next_id += 1
        bullet = PlaybookBullet(
            bullet_id=bullet_id,
            section=section,
            content=content,
            created_epoch=epoch,
            last_used_epoch=epoch,
        )
        self.bullets.append(bullet)
        return bullet

    def find_by_content(self, content: str, threshold: float = 0.85) -> PlaybookBullet | None:
        """Find a semantically similar bullet (simple substring fallback)."""
        # Simple substring/whitespace-normalized match.
        # In production: use sentence embeddings + cosine similarity.
        normalized = re.sub(r"\s+", " ", content.strip().lower())
        for b in self.bullets:
            b_normalized = re.sub(r"\s+", " ", b.content.strip().lower())
            if normalized == b_normalized:
                return b
            # partial overlap heuristic
            words = set(normalized.split())
            b_words = set(b_normalized.split())
            if len(words) > 3 and len(b_words) > 3:
                overlap = len(words & b_words) / max(len(words | b_words), 1)
                if overlap >= threshold:
                    return b
        return None

    def record_success(self, bullet_ids: list[str]) -> None:
        """Increment helpful counter for referenced bullets."""
        for b in self.bullets:
            if b.bullet_id in bullet_ids:
                b.helpful += 1

    def record_failure(self, bullet_ids: list[str]) -> None:
        """Increment harmful counter for referenced bullets."""
        for b in self.bullets:
            if b.bullet_id in bullet_ids:
                b.harmful += 1

    def prune(self) -> int:
        """Remove bullets that are clearly harmful. Returns count removed."""
        before = len(self.bullets)
        self.bullets = [b for b in self.bullets if not b.is_harmful]
        return before - len(self.bullets)

    def deduplicate(self, threshold: float = 0.85) -> int:
        """Merge near-duplicate bullets (keep the one with higher score)."""
        removed = 0
        i = 0
        while i < len(self.bullets):
            j = i + 1
            while j < len(self.bullets):
                dup = self.find_by_content(self.bullets[j].content, threshold)
                if dup is not None and dup.bullet_id != self.bullets[j].bullet_id:
                    # Merge counters into the kept bullet.
                    dup.helpful += self.bullets[j].helpful
                    dup.harmful += self.bullets[j].harmful
                    self.bullets.pop(j)
                    removed += 1
                else:
                    j += 1
            i += 1
        return removed

    def enforce_budget(self) -> int:
        """Trim to max_tokens by removing lowest-scoring bullets. Returns trimmed count."""
        if not self.bullets:
            return 0
        total = sum(len(b.content) for b in self.bullets)
        if total <= self.max_tokens:
            return 0
        # Sort by score ascending, remove worst.
        sorted_bullets = sorted(self.bullets, key=lambda b: b.score)
        removed = 0
        while total > self.max_tokens and len(sorted_bullets) > 1:
            b = sorted_bullets.pop(0)
            total -= len(b.content)
            self.bullets.remove(b)
            removed += 1
        return removed

    # ── Rendering ──────────────────────────────────────────────────

    def render(self, section_order: list[str] | None = None) -> str:
        """Render playbook as structured text block.

        Parameters
        ----------
        section_order:
            Order of sections. Default: strategies → mistakes → templates.
        """
        if not self.bullets:
            return "(no proof strategies collected yet)"

        if section_order is None:
            section_order = ["strategies", "mistakes", "templates"]

        lines: list[str] = []
        for section in section_order:
            section_bullets = [b for b in self.bullets if b.section == section]
            if not section_bullets:
                continue
            section_bullets.sort(key=lambda b: b.score, reverse=True)
            title = section.upper().replace("_", " & ")
            lines.append(f"\n## {title}")
            for bullet in section_bullets:
                lines.append(f"  {bullet.render()}")
        return "\n".join(lines) + "\n"

    def __len__(self) -> int:
        return len(self.bullets)


# ── Error Analyzer (Reflector role) ──────────────────────────────────


class ErrorAnalyzer:
    """Extract actionable lessons from T2 compilation diagnostics.

    Maps common Lean error patterns to structured playbook bullets.
    """

    # Pattern → (section, lesson_template)
    ERROR_PATTERNS: dict[str, tuple[str, str]] = {
        "unsolved goals": (
            "strategies",
            "Break down large goals into subgoals. Could also try ``aesop`` "
            "or ``nlinarith`` for arithmetic goals.",
        ),
        "expected '{'": (
            "mistakes",
            "Always wrap tactic blocks in ``:= by {{ ... }}``, not ``:= by`` alone.",
        ),
        "unknown identifier": (
            "mistakes",
            "Check that all imports are present and identifiers are spelled correctly.",
        ),
        "type mismatch": (
            "strategies",
            "Use ``convert`` to bridge type mismatches when the goal is close but not exact.",
        ),
        "don't know how to synthesize": (
            "mistakes",
            "Provide explicit type annotations when Lean cannot infer them.",
        ),
        "application type mismatch": (
            "strategies",
            "Check argument order and number of arguments in function application.",
        ),
        "invalid field notation": (
            "mistakes",
            "A structure/field notation requires the structure type to be in scope.",
        ),
        "unknown namespace": (
            "mistakes",
            "Open the required namespace with ``open`` or use fully qualified names.",
        ),
    }

    def __init__(self) -> None:
        self._compiled: list[tuple[re.Pattern, str, str]] = [
            (re.compile(pattern, re.IGNORECASE), section, lesson)
            for pattern, (section, lesson) in self.ERROR_PATTERNS.items()
        ]

    @classmethod
    def extract(  # noqa: ARG001
        cls,
        attempt: str,  # noqa: ARG003
        diagnostics: list[dict[str, Any]],
        epoch: int = 0,
    ) -> list[PlaybookBullet]:
        """Convert T2 diagnostics into playbook bullets.

        Parameters
        ----------
        attempt:
            The Lean code that was compiled.
        diagnostics:
            Parsed T2 diagnostics (``[{"message": ..., "severity": ...}, ...]``).
        epoch:
            Current iteration epoch for tracking.

        Returns
        -------
        list[PlaybookBullet]
            New lessons extracted from this compilation.
        """
        analyzer = cls()
        bullets: list[PlaybookBullet] = []
        seen_contents: set[str] = set()

        for diag in diagnostics:
            msg = diag.get("message", "")
            for pattern, section, lesson in analyzer._compiled:
                if pattern.search(msg) and lesson not in seen_contents:
                    seen_contents.add(lesson)
                    bullets.append(
                        PlaybookBullet(
                            bullet_id="",  # assigned by Playbook.add_bullet
                            section=section,
                            content=lesson,
                            created_epoch=epoch,
                        )
                    )
                    break  # first match only per diagnostic

        return bullets


# ── Playbook Manager (Curator role) ──────────────────────────────────


class PlaybookManager:
    """Manages the proof playbook lifecycle: update, inject, compile.

    Integrates with GoedelProver's self-correction loop:
    - After T2: ``update_from_result()`` → maybe creates new bullets
    - Before next attempt: ``inject_into_prompt()`` → playbook in context
    """

    def __init__(self, playbook: Playbook | None = None) -> None:
        self.playbook = playbook or Playbook()
        self._epoch = 0

    def update_from_result(  # noqa: ARG001
        self,
        theorem: str,  # noqa: ARG002
        attempt: str,
        diagnostics: list[dict[str, Any]],
        succeeded: bool,
        referenced_bullets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Process a T2 compile result and update the playbook.

        Parameters
        ----------
        theorem:
            Theorem header for logging.
        attempt:
            Lean code that was compiled.
        diagnostics:
            Parsed T2 diagnostics.
        succeeded:
            Whether compilation succeeded.
        referenced_bullets:
            Bullet IDs that were referenced from the prompt when generating
            this attempt.

        Returns
        -------
        dict
            Summary of changes: bullets added, helpful/harmful updates, etc.
        """
        self._epoch += 1
        result: dict[str, Any] = {
            "bullets_added": 0,
            "helpful_updates": 0,
            "harmful_updates": 0,
            "pruned": 0,
        }

        # Track which referenced bullets were helpful vs harmful.
        if referenced_bullets:
            if succeeded:
                self.playbook.record_success(referenced_bullets)
                result["helpful_updates"] = len(referenced_bullets)
            else:
                self.playbook.record_failure(referenced_bullets)
                result["harmful_updates"] = len(referenced_bullets)

        # Extract new lessons from errors.
        if not succeeded and diagnostics:
            new_bullets = ErrorAnalyzer.extract(attempt, diagnostics, epoch=self._epoch)
            for bullet in new_bullets:
                existing = self.playbook.find_by_content(bullet.content)
                if existing is None:
                    self.playbook.add_bullet(
                        section=bullet.section,
                        content=bullet.content,
                        epoch=self._epoch,
                    )
                    result["bullets_added"] += 1
                # else: already in playbook, no duplicate

        # Lazy maintenance.
        result["pruned"] = self.playbook.prune()

        if result["bullets_added"] > 0 or result["pruned"] > 0:
            logger.info(
                "Playbook updated: +%d bullets, -%d pruned, %d total, epoch=%d",
                result["bullets_added"],
                result["pruned"],
                len(self.playbook),
                self._epoch,
            )

        return result

    def inject_into_prompt(self, prompt_template: str) -> str:
        """Replace ``{{PLAYBOOK}}`` placeholder with rendered playbook."""
        rendered = self.playbook.render()
        return prompt_template.replace("{{PLAYBOOK}}", rendered)

    def get_stats(self) -> dict[str, Any]:
        """Return playbook statistics."""
        sections: dict[str, int] = {}
        for b in self.playbook.bullets:
            sections[b.section] = sections.get(b.section, 0) + 1
        return {
            "total_bullets": len(self.playbook),
            "sections": sections,
            "epoch": self._epoch,
            "total_tokens_approx": sum(len(b.content) for b in self.playbook.bullets),
        }


# ── DSPy adaptation stub ─────────────────────────────────────────────


def compile_with_dspy(  # noqa: ARG001
    train_theorems: list[tuple[str, str]],
    val_theorems: list[tuple[str, str]],
    metric: str = "t2_pass",  # noqa: ARG001
    optimizer: str = "MIPROv2",
) -> dict[str, Any]:
    """(Stub) Optimise prompt templates using DSPy MIPROv2.

    Parameters
    ----------
    train_theorems:
        List of ``(theorem_header, expected_lean_code)`` for training.
    val_theorems:
        List of ``(theorem_header, expected_lean_code)`` for validation.
    metric:
        Optimisation metric (default: T2 compile pass rate).
    optimizer:
        DSPy optimizer to use: ``"MIPROv2"``, ``"GEPA"``, or
        ``"BootstrapFewShot"``.

    Returns
    -------
    dict
        Compiled program and optimisation statistics.

    Notes
    -----
    Requires ``dspy`` package. Falls back gracefully when unavailable.
    Implementation planned for Phase B.
    """
    try:
        import dspy  # pyright: ignore[reportMissingImports]  # noqa: F401
    except ImportError:
        logger.warning("dspy is not installed. Run ``pip install dspy`` for prompt optimisation.")
        return {"status": "skipped", "reason": "dspy not installed"}

    logger.info(
        "DSPy compile: %d train / %d val theorems, optimizer=%s",
        len(train_theorems),
        len(val_theorems),
        optimizer,
    )
    # Placeholder — actual DSPy compile loop:
    #   program = ProposerModule()
    #   compiled = MIPROv2(metric=t2_pass_rate).compile(
    #       program, trainset=train_set,
    #   )
    return {
        "status": "stub",
        "train_count": len(train_theorems),
        "val_count": len(val_theorems),
        "optimizer": optimizer,
    }
