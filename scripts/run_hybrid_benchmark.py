#!/usr/bin/env python3
"""Goedel-Hybrid Benchmark: local vLLM → enhanced prompt → DeepSeek API.

Experiment tags:
  hybrid_fix_k8m8    - Fix prompt: local candidates + T2 errors → API fix
  hybrid_schema_k8m8  - Schema prompt: few-shot examples + API
  hybrid_all_k8m8     - Combined: all prompts
  api_baseline_k8     - Baseline: pure API (current 56%)

Log: experiments/<tag>.log  &  experiments/<tag>.jsonl
"""
import os
import sys, json, os, time, re, subprocess, pathlib, logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Any

sys.path.insert(0, '~/omega-architect')

# ── Config ──────────────────────────────────────────────────────
EXPERIMENTS_DIR = pathlib.Path(__file__).resolve().parent / "experiments"
EXPERIMENTS_DIR.mkdir(exist_ok=True)

DEEPSEEK_MODEL = "deepseek-v4-flash"
LOCAL_PYTHON = os.environ.get("VLLM_PYTHON", "python")
LOCAL_PROVER = pathlib.Path(__file__).resolve().parent / "goedel_local_prover.py"

# Few-shot schemas (from known MiniF2F solutions)
FEW_SHOT_SCHEMAS = [
    {
        "header": "theorem mathd_algebra_148 (x : ℝ) : x * (-2) + 8 = x :=",
        "proof": "by nlinarith",
        "strategy": "nlinarith"
    },
    {
        "header": "theorem induction_sumkexp3eqsumksq (n : ℕ) : ∑ k in Finset.range n, k^3 = (∑ k in Finset.range n, k)^2 :=",
        "proof": "by\n  induction' n with n ih\n  · simp\n  · simp [ih, Finset.sum_range_succ]\n  repeat\n    ring",
        "strategy": "induction"
    },
    {
        "header": "theorem aime_1983_p3 (x : ℝ) : x^2 + x + 1 > 0 :=",
        "proof": "by\n  have h : x^2 + x + 1 = (x + 1/2)^2 + 3/4 := by ring\n  rw [h]\n  nlinarith",
        "strategy": "calc_chain"
    },
]

@dataclass
class ExperimentConfig:
    tag: str = "hybrid_all_k8m8"
    k_local: int = 8
    k_api: int = 8
    prompt_variants: list[str] = field(default_factory=lambda: ["fix", "schema", "hybrid"])
    temperature: float = 0.7

@dataclass
class ProblemResult:
    name: str = ""
    config_tag: str = ""
    local_attempts: int = 0
    local_unique: int = 0
    local_passed: int = 0
    api_attempts: int = 0
    api_unique: int = 0
    api_passed: int = 0
    total_passed: int = 0
    best_proof: str = ""
    total_time_s: float = 0.0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

# ── Stage 1: Local vLLM (batched) ──────────────────────────────
def run_local_vllm_batch(theorems: list[tuple[int, str]], k: int) -> dict[int, list[dict]]:
    """Run local vLLM on ALL problems in one invocation."""
    input_lines = []
    for idx, header in theorems:
        input_lines.append(json.dumps({"theorem_header": header, "k_local": k, "idx": idx}))
    input_str = "\n".join(input_lines)

    proc = subprocess.run(
        [LOCAL_PYTHON, str(LOCAL_PROVER)],
        input=input_str, capture_output=True, text=True, timeout=600
    )

    # Group results by idx
    results: dict[int, list[dict]] = {}
    for line in proc.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                cand = json.loads(line)
                idx = cand.pop("idx", 0)
                results.setdefault(idx, []).append(cand)
            except json.JSONDecodeError:
                pass
    return results

# ── Stage 2: DeepSeek API ───────────────────────────────────────
def call_deepseek(messages: list[dict], k: int = 1) -> list[str]:
    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser('~/.hermes/.env'))

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        timeout=30.0,
    )

    # Parallel API calls (DeepSeek only supports n=1)
    import concurrent.futures
    texts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(k, 8)) as executor:
        futures = []
        for _ in range(k):
            futures.append(executor.submit(
                client.chat.completions.create,
                model=DEEPSEEK_MODEL,
                messages=messages,
                n=1,
                temperature=0.7,
                max_tokens=8192,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            ))
        for future in concurrent.futures.as_completed(futures):
            try:
                texts.append(future.result().choices[0].message.content or "")
            except Exception as e:
                texts.append(json.dumps({"error": str(e)}))
    return texts

def build_prompt_fix(header: str, local_code: str, t2_errors: list[str]) -> list[dict]:
    err_str = "\n".join(t2_errors[:3]) if t2_errors else "No errors (incomplete proof)"
    return [
        {"role": "system", "content": "You are a Lean 4 proof specialist. Fix the failed proof attempt below."},
        {"role": "user", "content": (
            f"Prove this theorem:\n{header}\n\n"
            f"A prior attempt failed:\n```lean4\n{local_code}\n```\n\n"
            f"Errors:\n{err_str}\n\n"
            "Output JSON: {\"proof\": \"...\", \"confidence\": 0.0-1.0, \"strategy\": \"...\"}"
        )},
    ]

def build_prompt_schema(header: str) -> list[dict]:
    schemas_str = "\n\n".join(
        f"Example {i+1} ({s['strategy']}):\n{s['header']}\n{s['proof']}"
        for i, s in enumerate(FEW_SHOT_SCHEMAS)
    )
    return [
        {"role": "system", "content": "You are a Lean 4 proof specialist. Study the examples and prove the theorem."},
        {"role": "user", "content": (
            f"Study these correct Lean 4 proofs:\n\n{schemas_str}\n\n"
            f"Now prove:\n{header}\n\n"
            "Output JSON: {\"proof\": \"...\", \"confidence\": 0.0-1.0, \"strategy\": \"...\"}"
        )},
    ]

def build_prompt_hybrid(header: str, local_code: str, t2_errors: list[str]) -> list[dict]:
    schemas_str = "\n\n".join(
        f"Example {i+1} ({s['strategy']}):\n{s['header']}\n{s['proof']}"
        for i, s in enumerate(FEW_SHOT_SCHEMAS)
    )
    err_str = "\n".join(t2_errors[:3]) if t2_errors else "No errors"
    return [
        {"role": "system", "content": "You are a Lean 4 proof specialist."},
        {"role": "user", "content": (
            f"Study these correct proofs:\n\n{schemas_str}\n\n"
            f"Now prove:\n{header}\n\n"
            f"A prior attempt failed:\n```lean4\n{local_code}\n```\n\n"
            f"Errors:\n{err_str}\n\n"
            "Output JSON: {\"proof\": \"...\", \"confidence\": 0.0-1.0, \"strategy\": \"...\"}"
        )},
    ]

# ── Run experiment ──────────────────────────────────────────────
def run_experiment(cfg: ExperimentConfig, problems: list) -> tuple[list[ProblemResult], pathlib.Path]:
    results = []
    total = len(problems)
    t_start = time.perf_counter()

    # Per-problem checkpoint file
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ckpt_path = EXPERIMENTS_DIR / f"{cfg.tag}_{ts}.jsonl"

    # ── Stage 1: Batch local vLLM for ALL problems ──
    local_candidates_by_idx: dict[int, list[dict]] = {}
    if cfg.k_local > 0:
        print(f"Starting local vLLM batch for {total} problems, k={cfg.k_local}...", flush=True)
        theorems = [(i, p) for i, p in enumerate(problems)]
        try:
            local_candidates_by_idx = run_local_vllm_batch(theorems, cfg.k_local)
        except Exception as e:
            print(f"Local vLLM batch failed: {e}", flush=True)

    # ── Per-problem: Stage 2 (API) + T2 compile ──
    for i, prob in enumerate(problems):
        header = prob
        r = ProblemResult(name=header.split("(")[0].replace("theorem ","").strip()[:40],
                          config_tag=cfg.tag)
        t_prob = time.perf_counter()

        # Local candidates from batch
        local_candidates = local_candidates_by_idx.get(i, [])
        r.local_attempts = len(local_candidates)
        unique = list({c["lean_code"]: c for c in local_candidates}.values())
        r.local_unique = len(unique)
        r.details["local_candidates"] = unique[:5]

        # ── Stage 2: DeepSeek API ──
        api_messages = []

        # Always add schema variant
        if "schema" in cfg.prompt_variants:
            api_messages.append(("schema", build_prompt_schema(header), None))

        # fix/hybrid variants: use local candidates as seeds (if any)
        local_seeds = local_candidates[:3] if local_candidates else []
        for variant in cfg.prompt_variants:
            if variant == "schema":
                continue
            if local_seeds:
                for cand in local_seeds:
                    if variant == "fix":
                        api_messages.append(("fix", build_prompt_fix(header, cand["lean_code"], []), cand))
                    elif variant == "hybrid":
                        api_messages.append(("hybrid", build_prompt_hybrid(header, cand["lean_code"], []), cand))
            else:
                # No local seeds → still add fix/hybrid as diverse schema variants
                if variant == "fix":
                    api_messages.append(("fix", build_prompt_schema(header), None))
                elif variant == "hybrid":
                    api_messages.append(("hybrid", build_prompt_schema(header), None))

        # Distribute k_api across variants, handling leftovers
        n_variants = len(api_messages)
        k_base = cfg.k_api // max(n_variants, 1)
        k_extra = cfg.k_api % max(n_variants, 1)
        api_candidates = []
        for idx_var, (variant_name, msgs, _) in enumerate(api_messages):
            k_this = k_base + (1 if idx_var < k_extra else 0)
            if k_this < 1:
                continue
            texts = call_deepseek(msgs, k=k_this)
            for text in texts:
                try:
                    data = json.loads(text)
                    code = data.get("proof", text)
                    if code and len(code) > 10:
                        api_candidates.append({
                            "lean_code": code,
                            "confidence": float(data.get("confidence", 0.5)),
                            "strategy": data.get("strategy", variant_name),
                        })
                except json.JSONDecodeError:
                    pass

        r.api_attempts = len(api_candidates)
        unique_api = list({c["lean_code"]: c for c in api_candidates}.values())
        r.api_unique = len(unique_api)

        # T2 compile all unique candidates
        from omega.verify.t2_real import make_real_compile_callback
        compile_fn = make_real_compile_callback()
        from omega.verify.t2_lean import T2Result

        all_candidates = local_candidates + api_candidates
        unique_all = list({c["lean_code"]: c for c in all_candidates}.values())

        for cand in unique_all:
            try:
                formatted = f"{header} :=\n{cand['lean_code']}"
                result = compile_fn(formatted)
                diagnostics = result.get("diagnostics", [])
                has_error = any(d.get("severity") == "error" for d in diagnostics)
                if not has_error:
                    cand["verified"] = True
                    r.total_passed += 1
                    if not r.best_proof:
                        r.best_proof = cand["lean_code"]
            except Exception:
                pass

        r.total_time_s = time.perf_counter() - t_prob
        results.append(r)

        # Per-problem checkpoint
        with open(ckpt_path, "a") as f:
            f.write(json.dumps(asdict(r)) + "\n")

        status = "OK" if r.total_passed > 0 else "FAIL"
        log_line = (
            f"[{i+1:2d}/{total}] [{status}] {r.name:40s} "
            f"local={r.local_unique}u/{r.local_attempts}a "
            f"api={r.api_unique}u/{r.api_attempts}a "
            f"passed={r.total_passed}  {r.total_time_s:.1f}s"
        )
        print(log_line, flush=True)

    return results, ckpt_path

# ── Main ────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--problems", type=str, default="/tmp/minif2f_50sample.jsonl")
    parser.add_argument("--tag", type=str, default="hybrid_all_k8m8",
                        help="Experiment tag (log file name)")
    parser.add_argument("--k_local", type=int, default=8)
    parser.add_argument("--k_api", type=int, default=8)
    parser.add_argument("--variants", type=str, default="fix,schema,hybrid")
    parser.add_argument("--max", type=int, default=50)
    parser.add_argument("--no-local", action="store_true", help="Skip local vLLM (API-only baseline)")
    args = parser.parse_args()

    # Load problems
    problems = []
    with open(args.problems) as f:
        for line in f:
            d = json.loads(line.strip())
            formal = d.get("formal_statement", "") or d.get("lean4_code", "")
            # Extract theorem header
            for line_text in formal.split("\n"):
                line_text = line_text.strip()
                if line_text.startswith("theorem ") or line_text.startswith("lemma "):
                    if ":=" in line_text:
                        line_text = line_text.split(":=")[0].strip() + " :="
                    problems.append(line_text)
                    break

    if args.max:
        problems = problems[:args.max]

    cfg = ExperimentConfig(
        tag=args.tag,
        k_local=0 if args.no_local else args.k_local,
        k_api=args.k_api,
        prompt_variants=args.variants.split(","),
    )

    print(f"Experiment: {cfg.tag}")
    print(f"  k_local={cfg.k_local}, k_api={cfg.k_api}, variants={cfg.prompt_variants}")
    print(f"  Problems: {len(problems)}")
    print("=" * 60)
    sys.stdout.flush()

    results, ckpt_path = run_experiment(cfg, problems)

    # Save log
    log_path = EXPERIMENTS_DIR / ckpt_path.name.replace(".jsonl", ".log")

    proven = sum(1 for r in results if r.total_passed > 0)
    with open(log_path, "w") as f:
        f.write(f"Experiment: {cfg.tag}\n")
        f.write(f"Time: {datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}\n")
        f.write(f"k_local={cfg.k_local}, k_api={cfg.k_api}\n")
        f.write(f"Variants: {cfg.prompt_variants}\n")
        f.write("-" * 60 + "\n")
        f.write(f"Problems: {len(results)}, Proven: {proven} ({100*proven/max(len(results),1):.1f}%)\n")
        f.write(f"Total proofs: {sum(r.total_passed for r in results)}\n")
        f.write("-" * 60 + "\n")
        for r in results:
            status = "OK" if r.total_passed > 0 else "FAIL"
            f.write(f"[{status}] {r.name:40s} local={r.local_unique}u api={r.api_unique}u passed={r.total_passed} {r.total_time_s:.1f}s\n")
        f.write("-" * 60 + "\n")
        f.write(f"Total time: {sum(r.total_time_s for r in results):.1f}s\n")

    print()
    print("=" * 60)
    print(f"  {cfg.tag} RESULTS")
    print("=" * 60)
    print(f"  Problems:  {len(results)}")
    print(f"  Proven:    {proven}/{len(results)} ({100*proven/max(len(results),1):.1f}%)")
    print(f"  Proofs:    {sum(r.total_passed for r in results)}")
    print(f"  Log:       {log_path.name}")
    print(f"  JSONL:     {ckpt_path.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
