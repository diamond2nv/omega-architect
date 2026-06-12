"""Run Hybrid v2 on imo_1959_p1 to test stuck→sampling→retry path."""
import sys, json, time, pathlib

sys.dont_write_bytecode = True
import omega.resource.config, omega.resource.budget, omega.resource.tracker

from omega.engine.hybrid import run_hybrid_v2, HybridV2Config

DATASET = pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / \
    "formal-provers" / "goedel-prover-v2" / "dataset" / "minif2f.jsonl"

name = "imo_1959_p1"
statement = ""
with open(DATASET) as f:
    for line in f:
        data = json.loads(line)
        if data.get("name") == name:
            statement = data.get("formal_statement", "")
            break

t0 = time.time()
cfg = HybridV2Config(
    phase1_rounds=12,       # Stop early if stuck
    phase1_timeout_s=90,    # 90s per phase
    phase2_samples=6,
    phase2_corrections=1,
    phase3_rounds=15,
)
result = run_hybrid_v2(statement, cfg)
elapsed = time.time() - t0

print(f"success={result.success}", flush=True)
print(f"source={result.source}", flush=True)
print(f"attempts={result.n_attempts}", flush=True)
print(f"time={elapsed:.1f}s", flush=True)
print(f"stuck_reason={result.stuck_reason}", flush=True)
print(f"proof_len={len(result.proof or '')}", flush=True)
if result.phase1_result:
    print(f"p1_rounds={result.phase1_result.rounds}", flush=True)
    print(f"p1_termination={result.phase1_result.termination}", flush=True)
print(f"p2_enabled={result.phase2_result is not None}", flush=True)
print(f"p3_enabled={result.phase3_result is not None}", flush=True)
