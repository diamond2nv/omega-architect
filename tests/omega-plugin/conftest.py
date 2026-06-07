"""pytest conftest for omega-plugin bridge tests.

Loads ``_bridge.py`` via importlib to bypass the hyphen in the
``omega-plugin`` directory name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_bridge():
    """Load _bridge.py via importlib (handles hyphen in dir name)."""
    plugin_dir = Path(__file__).resolve().parents[2] / "omega-plugin"
    bridge_path = plugin_dir / "_bridge.py"

    spec = importlib.util.spec_from_file_location(
        "omega_plugin_bridge",
        str(bridge_path),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["omega_plugin_bridge"] = module
    spec.loader.exec_module(module)
    return module


# Load once at module level
bridge = _load_bridge()
