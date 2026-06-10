#!/usr/bin/env python3
"""Ω-Architect core package.

Version is maintained in pyproject.toml — single source of truth.
"""

from __future__ import annotations

import importlib.metadata
import warnings

try:
    __version__ = importlib.metadata.version("omega-architect")
except importlib.metadata.PackageNotFoundError:
    # Fallback for editable installs / dev mode
    try:
        import tomllib  # Python 3.11+

        with open("pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        __version__ = data.get("project", {}).get("version", "0.0.0")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        __version__ = "0.0.0-dev"
        warnings.warn("Could not determine version from pyproject.toml")
