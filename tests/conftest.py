"""pytest configuration for omega-architect tests."""
from __future__ import annotations

import shutil
import subprocess

import pytest


def _has_lean() -> bool:
    """Check if Lean 4 + lake toolchain is available."""
    if not shutil.which("lake"):
        return False
    if not shutil.which("lean"):
        return False
    return True


def _has_gpu() -> bool:
    """Check if CUDA GPU is available."""
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# pytest markers
needs_lean = pytest.mark.skipif(not _has_lean(), reason="Lean 4 toolchain not available")
needs_gpu = pytest.mark.skipif(not _has_gpu(), reason="No NVIDIA GPU / CUDA available")
