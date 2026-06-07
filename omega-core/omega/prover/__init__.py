#!/usr/bin/env python3
"""Ω-Architect Prover Module — proof generation engines.

Three prover strategies + ensemble orchestrator:
- **Goedel**: Parallel sampling + self-correction via T2 errors
- **Rethlas**: Blueprint decomposition + recursive subgoal solving
- **Archon**: Multi-strategy ensemble with progress critic
- **Ensemble**: Run all three, compare and elect best proof
"""

from omega.prover.ar_prover import ArchonProver, CriticStatus, ProgressCritic
from omega.prover.ensemble import EnsembleProver, EnsembleResult
from omega.prover.go_prover import GoedelProver, GoedelResult
from omega.prover.re_prover import Blueprint, RethlasProver, Subgoal

__all__ = [
    "GoedelProver",
    "GoedelResult",
    "RethlasProver",
    "Blueprint",
    "Subgoal",
    "ArchonProver",
    "ProgressCritic",
    "CriticStatus",
    "EnsembleProver",
    "EnsembleResult",
]
