#!/usr/bin/env python3
"""Ω-Architect core package.

PEP 621: pyproject.toml is the single source of truth.
The sentinel "0.0.0" signals the editable/uninstalled case.
"""

__version__ = "0.0.0"

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("omega-core")
except Exception:
    pass  # Keep sentinel
