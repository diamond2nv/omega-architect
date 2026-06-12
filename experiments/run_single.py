"""Single theorem runner for Hybrid vs Dialogue experiment.
Usage: python3 run_single.py <theorem_name> <mode:dialogue|hybrid> <timeout_seconds>
"""
import sys, json, time, pathlib

sys.dont_write_bytecode = True

# Preload in safe order
import omega.resource.config
import omega.resource.budget
import omega.resource.tracker

from omega.engine.hybrid import run_hybrid, HybridConfig
from omega.loop.inner import inner_loop, InnerLoopConfig

DATASET = pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / \
    "formal-provers" / "goedel-prover-v2" / "dataset" / "minif2f.jsonl"

name = sys.argv[1]
mode = sys.argv[2]
timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else 120

# Load theorem
statement = ""
with open(DATASET) as f:
    for line in f:
        data = json.loads(line)
        if data.get("name") == name:
            statement = data.get("formal_statement", "")
            break

if not statement:
    print(json.dumps({"name": name, "mode": mode, "success": False, "error": "NOT FOUND"}))
    sys.exit(1)

t0 = time.time()

if mode == "dialogue":
    cfg = InnerLoopConfig(max_rounds=30)
    r = inner_loop(theorem_header=statement, config=cfg)
    elapsed = time.time() - t0
    result = {
        "name": name, "mode": mode,
        "success": r.success, "attempts": r.rounds,
        "time_s": round(elapsed, 1),
        "proof_len": len(r.code or ""),
        "error": (r.error or "")[:200],
    }
elif mode == "hybrid":
    cfg = HybridConfig(num_samples=4, max_correction_rounds=1, max_dialogue_rounds=20)
    r = run_hybrid(theorem_header=statement, config=cfg)
    elapsed = time.time() - t0
    result = {
        "name": name, "mode": mode,
        "success": r.success, "source": r.source,
        "attempts": r.n_attempts,
        "time_s": round(elapsed, 1),
        "proof_len": len(r.proof or ""),
        "error": "",
    }

print(json.dumps(result))
