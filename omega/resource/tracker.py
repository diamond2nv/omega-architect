"""ConvergenceTracker — epoch-level progress monitoring for proof correction rounds.

Tracks error counts, proof length, and convergence rate across epochs.
Provides stuck/diverging detection for the correction loop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class EpochSnapshot:
    """Snapshot of a single epoch's metrics."""

    epoch: int
    n_errors: int
    proof_length: int
    elapsed_s: float
    error_rate: float = field(init=False)
    error_signature: str = field(init=False)
    convergence_rate: float = 0.0

    def __post_init__(self) -> None:
        self.error_rate = self.n_errors / max(self.proof_length, 1)

    @staticmethod
    def _make_signature(errors: list[str]) -> str:
        """Create a short hash from the first error text for change detection."""
        if not errors:
            return "no-errors"
        raw = errors[0][:200]
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    @classmethod
    def create(
        cls,
        epoch: int,
        n_errors: int,
        proof_length: int,
        elapsed_s: float,
        errors: list[str],
        convergence_rate: float = 0.0,
    ) -> EpochSnapshot:
        """Factory method that computes derived fields automatically."""
        snap = cls(
            epoch=epoch,
            n_errors=n_errors,
            proof_length=proof_length,
            elapsed_s=elapsed_s,
        )
        snap.convergence_rate = convergence_rate
        snap.error_signature = cls._make_signature(errors)
        return snap


class ConvergenceTracker:
    """Tracks epoch-level convergence of proof correction rounds.

    Convergence rate measures how fast errors are being eliminated.
    A rate of 1.0 = fast convergence, 0.0 = stuck, negative = diverging.
    """

    def __init__(
        self,
        window: int = 3,
        convergence_threshold: float = 0.1,
    ) -> None:
        self._window = max(window, 2)
        self._threshold = convergence_threshold
        self._epochs: list[EpochSnapshot] = []
        self._convergence_rates: list[float] = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_epoch(
        self,
        n_errors: int,
        proof_length: int,
        elapsed_s: float,
        errors: list[str],
    ) -> EpochSnapshot:
        """Record a new epoch and return its snapshot.

        Convergence rate is computed from the last *window* epochs.
        """
        epoch = len(self._epochs) + 1

        rate = self._compute_rate(n_errors)
        snap = EpochSnapshot.create(
            epoch=epoch,
            n_errors=n_errors,
            proof_length=proof_length,
            elapsed_s=elapsed_s,
            errors=errors,
            convergence_rate=rate,
        )

        self._epochs.append(snap)
        self._convergence_rates.append(rate)
        return snap

    def _compute_rate(self, current_errors: int) -> float:
        """Compute the current convergence rate.

        Rate = (prev_errors - current_errors) / max(prev_errors, 1).
        A positive rate means errors are decreasing (good).
        A negative rate means errors are increasing (diverging).
        Zero means no change (stuck).
        """
        if not self._epochs:
            # First epoch — no rate yet
            return 0.0

        prev = self._epochs[-1].n_errors
        if prev == 0:
            # Already zero errors — everything is converged
            return 1.0

        delta = prev - current_errors
        return delta / prev

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def convergence_rate(self) -> float:
        """Return the latest convergence rate (1.0 = fast, 0.0 = stuck, negative = diverging).

        If fewer than *window* epochs exist, returns the current rate as-is.
        """
        if not self._convergence_rates:
            return 0.0
        return self._convergence_rates[-1]

    def is_converged(self) -> bool:
        """Return True if the latest epoch(s) indicate convergence.

        Converged when:
        1. Zero errors in the latest epoch, OR
        2. The recent average convergence rate is below *threshold*
           (i.e. error reduction has stalled — we're as good as we'll get).
        """
        if not self._epochs:
            return False

        latest = self._epochs[-1]
        if latest.n_errors == 0:
            return True

        # Check if the average rate over the window is below threshold
        recent = self._recent_rates()
        if not recent:
            return False
        avg_rate = sum(recent) / len(recent)
        return 0.0 < avg_rate < self._threshold

    def is_stuck(self) -> bool:
        """Return True if convergence has stalled (no improvement over the window)."""
        recent = self._recent_rates()
        if len(recent) < 2:
            return False
        return all(r <= 0.0 for r in recent[-2:])

    def is_diverging(self) -> bool:
        """Return True if errors are increasing (negative convergence rate)."""
        recent = self._recent_rates()
        if not recent:
            return False
        return recent[-1] < 0.0

    def best_epoch(self) -> int:
        """Return the epoch number with the fewest errors (tie-break: shortest proof)."""
        if not self._epochs:
            return 0

        best = min(
            self._epochs,
            key=lambda e: (e.n_errors, e.proof_length),
        )
        return best.epoch

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recent_rates(self) -> list[float]:
        """Return convergence rates for the last *window* epochs."""
        return self._convergence_rates[-self._window :]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a human-readable convergence summary."""
        if not self._epochs:
            return "ConvergenceTracker — no epochs recorded"

        latest = self._epochs[-1]
        best_ep = self.best_epoch()
        best_snap = (
            next(e for e in self._epochs if e.epoch == best_ep)
            if best_ep > 0
            else None
        )

        lines = [
            f"ConvergenceTracker — {len(self._epochs)} epoch(s)",
            f"  latest epoch: #{latest.epoch} — errors={latest.n_errors}, "
            f"length={latest.proof_length}, error_rate={latest.error_rate:.3f}",
            f"  convergence rate: {self.convergence_rate():.3f}  "
            f"{'✓' if self.is_converged() else '…'} "
            f"{'⚠ stuck' if self.is_stuck() else ''} "
            f"{'⚠ diverging' if self.is_diverging() else ''}",
            f"  best epoch: #{best_ep} "
            f"(errors={best_snap.n_errors if best_snap else '?'})",
        ]

        if self._epochs:
            # Show error signature change detection
            sigs = {e.error_signature for e in self._epochs}
            lines.append(f"  unique error signatures: {len(sigs)}")

        return "\n".join(lines)

    def __repr__(self) -> str:
        rate = self.convergence_rate()
        status = "converged" if self.is_converged() else "diverging" if self.is_diverging() else "stuck" if self.is_stuck() else "running"
        return (
            f"ConvergenceTracker(epochs={len(self._epochs)}, "
            f"rate={rate:.3f}, status={status})"
        )
