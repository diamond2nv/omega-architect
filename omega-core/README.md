# omega-core

**Zero-dependency Python package** for open formal theorem proving.

- `pip install omega-core` — pure Python, no external deps
- Proves Lean 4 theorems via three parallel proof engines (Goedel, Rethlas, Archon)
- T2 verification via system `lean` compiler (subprocess)
- LLM providers injected via `generate_fn` callback — bring your own model

## Quick Start

```python
from omega.prover import GoedelProver
from omega.verify.t2_lean import verify

# Inject your LLM generate function
def my_generate_fn(prompt: str) -> str:
    # Call your LLM here (Ollama, OpenAI, Hermes ctx.llm, ...)
    return "... Lean proof attempt ..."

prover = GoedelProver(
    compile_fn=verify,
    generate_fn=my_generate_fn,
    num_samples=6,
)
result = prover.run("theorem add_zero (n : ℕ) : n + 0 = n :=")
if result.proof:
    print(f"Found proof in {result.timings['total_s']:.2f}s")
```

## Architecture

```
omega-core/
├── search/        # ProofTree, GoalState, Proposer
├── prover/        # GoedelProver, RethlasProver, ArchonProver, Ensemble
├── verify/        # T1 regex check, T2 Lean compiler
└── resource/      # Budget tracking, convergence monitoring
```

All LLM calls go through a `generate_fn: Callable[[str], str]` callback.
All T2 compilation goes through a `compile_fn: Callable[[str], dict]` callback.
No API keys, no .env, no provider configuration in core.

## License

Apache 2.0
