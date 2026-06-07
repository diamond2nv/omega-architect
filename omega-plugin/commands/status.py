"""omega-plugin/commands/status.py — ``/omega-status`` slash command.

Shows the current environment status: omega-core, Lean compiler, MCP.
"""

from __future__ import annotations

import logging

from .._bridge import OmegaBridge

logger = logging.getLogger("omega-plugin.status")


def register_status_command(ctx) -> None:
    """Register the ``/omega-status`` slash command."""

    def handle(_raw_args: str) -> str:
        """Handler for ``/omega-status`` — just returns environment info."""
        bridge = OmegaBridge(ctx)
        info = bridge.status()
        return bridge.format_status_for_chat(info)

    ctx.register_command(
        name="omega-status",
        handler=handle,
        description="Show Ω-Architect plugin status: omega-core, Lean, MCP",
        args_hint="",
    )
