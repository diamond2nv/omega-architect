"""omega-plugin — Ω-Architect Hermes Plugin.

Provides four slash commands:

- ``/omega-prove`` — Prove a Lean theorem
- ``/omega-benchmark`` — Run MiniF2F benchmark
- ``/omega-learn`` — Analyse proof patterns (skeleton)
- ``/omega-status`` — Show environment status

All commands use ``ctx.llm.complete()`` for LLM interactions and
``omega-core`` for proof engine logic.
"""

from __future__ import annotations

import logging

from .commands.benchmark import register_benchmark_command
from .commands.learn import register_learn_command
from .commands.prove import register_prove_command
from .commands.status import register_status_command

logger = logging.getLogger("omega-plugin")


def register(ctx) -> None:
    """Called by Hermes at plugin load time.

    Registers all slash commands provided by the omega plugin.
    """
    logger.info("Registering Ω-Architect plugin commands...")

    register_prove_command(ctx)
    register_benchmark_command(ctx)
    register_learn_command(ctx)
    register_status_command(ctx)

    logger.info(
        "Ω-Architect plugin registered: omega-prove, omega-benchmark, "
        "omega-learn, omega-status"
    )
