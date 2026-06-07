"""omega-plugin/commands/learn.py — ``/omega-learn`` slash command.

Analyse successful proofs and learn reusable tactics/patterns.
This is a skeleton for future implementation — currently returns a
description of the feature.
"""

from __future__ import annotations

import logging

from .._bridge import OmegaBridge

logger = logging.getLogger("omega-plugin.learn")


def register_learn_command(ctx) -> None:
    """Register the ``/omega-learn`` slash command.

    This command is a placeholder for proof-pattern learning.
    In future versions it will:
    1. Read successful proofs from a local cache
    2. Extract common tactic sequences
    3. Build a Playbook for future proof attempts
    """

    def handle(raw_args: str) -> str:
        """Handler for ``/omega-learn [--from <file>] [--save <name>]``."""
        args = raw_args.strip()

        if not args:
            return _usage()

        bridge = OmegaBridge(ctx)
        info = bridge.status()
        if not info.get("omega_core"):
            return "omega-core not installed. Learn feature requires omega-core."

        # TODO: implement actual learning logic
        return (
            "🔄 /omega-learn is in development.\n\n"
            "Planned functionality:\n"
            "- /omega-learn --from <cache>  — analyse successful proofs\n"
            "- /omega-learn --save <name>   — persist extracted patterns as Playbook\n"
            "- /omega-learn --status        — show learned patterns\n"
            "\n"
            "To see current status: /omega-status"
        )

    ctx.register_command(
        name="omega-learn",
        handler=handle,
        description="Learn reusable proof patterns from successful results (in development)",
        args_hint="--from <file> | --save <name> | --status",
    )


def _usage() -> str:
    return (
        "Usage:\n"
        "  /omega-learn --from <cache>  — analyse successful proofs (TODO)\n"
        "  /omega-learn --save <name>   — persist patterns (TODO)\n"
        "  /omega-learn --status        — show learned patterns (TODO)\n"
        "\n"
        "This feature is under development. Check back in a future release."
    )
