"""Test helpers — shared markers and utilities for omega-architect tests."""

from __future__ import annotations

import shutil
import subprocess

import pytest


def has_lean() -> bool:
    """Check if Lean 4 + lake toolchain is available."""
    return bool(shutil.which("lake") and shutil.which("lean"))


def has_gpu() -> bool:
    """Check if CUDA GPU is available."""
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


needs_lean = pytest.mark.skipif(not has_lean(), reason="Lean 4 toolchain not available")
needs_gpu = pytest.mark.skipif(not has_gpu(), reason="No NVIDIA GPU / CUDA available")
