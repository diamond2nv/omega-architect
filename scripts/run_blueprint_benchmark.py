#!/usr/bin/env python3
"""Blueprint Benchmark — 端到端跑 Goedel-Architect 风格管线。

流程 (每题):
  1. blueprint: 用 DeepSeek API 将定理分解为 DAG (依赖图的 lemmas)
  2. prove: 对每个 lemma 跑 pass@k (DeepSeek API k=8, 并行)
  3. refine: 失败 lemma 触发精炼 (拆/改/补), 最多 3 轮
  4. 报告: 通过/失败 + 耗时 + 蓝图统计

Usage:
  python scripts/run_blueprint_benchmark.py --max 5 2>&1 | tee bp_run.log
"""

import json
import os
import sys
import time
import pathlib
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omega.search.blueprint import (
    Blueprint, LemmaNode, LemmaStatus, DiagnosisType,
    generate_blueprint, prove_blueprint, refine_blueprint,
)
from omega.search.passk import OmegaPassKManager


# ── Config ──────────────────────────────────────────────────

EXPERIMENTS_DIR = pathlib.Path(__file__).resolve().parent / "experiments"
EXPERIMENTS_DIR.mkdir(exist_ok=True)

K_PASS = 8          # pass@k per lemma
MAX_REFINEMENTS = 3  # 精炼轮次上限
MAX_PROBLEMS = 50   # 默认 50 题
BENCHMARK_FILE = "/tmp/minif2f_50sample.jsonl"


# ── LLM Wrapper (for blueprint gen + refine) ────────────────

def _search_matlas_for_blueprint(theorem_hint: str, top_k: int = 3) -> str:
    """Disabled — Matlas context breaks JSON mode in blueprint generation."""
    return ""


def make_llm_generate():
    """Create an llm_generate callable wired to DeepSeek API.

    Automatically searches Matlas for relevant mathematical context
    and injects it into the prompt for blueprint generation and refinement.
    """
    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser('~/.hermes/.env'))

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        timeout=120.0,
    )

    def llm_generate(prompt: str) -> str:
        # Inject Matlas search context into prompt
        # Extract theorem hint from prompt (first non-empty line with theorem/lemma/def)
        hint = ""
        for line in prompt.split("\n"):
            line = line.strip()
            if any(line.startswith(kw) for kw in ["theorem ", "lemma ", "def ", "Theorem:", "theorem_header"]):
                hint = line
                break
        if not hint:
            # Use first meaningful line
            hint = prompt.split("\n")[0][:200]

        matlas_ctx = _search_matlas_for_blueprint(hint, top_k=3)
        final_prompt = prompt
        if matlas_ctx:
            final_prompt = matlas_ctx + "\n\n" + prompt

        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": final_prompt}],
            n=1,
            temperature=0.3,
            max_tokens=8192,
            timeout=180,
            extra_body={"thinking": {"type": "disabled"}},
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"

    return llm_generate


# ── Per-problem runner ──────────────────────────────────────

def run_one_problem(
    header: str,
    passk_mgr: OmegaPassKManager,
    llm_gen: callable,
    k: int = K_PASS,
    max_refine: int = MAX_REFINEMENTS,
) -> dict:
    """Run full blueprint cycle on one theorem.

    Returns result dict with keys:
      name, header, succeeded, blueprint_lemmas, blueprint_refinements,
      rounds, total_time_s, errors
    """
    name = header.split("(")[0].replace("theorem ", "").strip()[:40]
    result = {
        "name": name,
        "header": header,
        "succeeded": False,
        "blueprint_lemmas": 0,
        "blueprint_refinements": 0,
        "rounds": [],
        "total_time_s": 0.0,
        "errors": [],
    }
    t_start = time.perf_counter()

    # ── Round 0: Blueprint Generation ──
    try:
        bp = generate_blueprint(header, llm_generate=llm_gen)
    except Exception as e:
        result["errors"].append(f"blueprint_gen: {e}")
        # Fallback: single-lemma blueprint
        bp = generate_blueprint(header, llm_generate=None)
    result["blueprint_lemmas"] = len(bp.lemmas)

    # ── Prove + Refine loop ──
    for round_idx in range(max_refine + 1):
        round_data = {"round": round_idx, "proved": 0, "failed": 0, "time_s": 0.0}

        # Prove all unproven lemmas
        t_round = time.perf_counter()
        bp = prove_blueprint(bp, passk_mgr, k=k)
        round_data["time_s"] = time.perf_counter() - t_round

        proved = len(bp.proved())
        failed = len(bp.failed())
        round_data["proved"] = proved
        round_data["failed"] = failed
        result["rounds"].append(round_data)

        if bp.all_proved:
            result["succeeded"] = True
            break

        # Dead-end detection: if R0 pass rate = 0% and ≥4 lemmas, skip refine
        if round_idx == 0 and round_data["proved"] == 0 and round_data["failed"] >= 4:
            result["errors"].append("Dead-end: 0% pass rate with 4+ lemmas, skipping refine")
            print(f"  ⏩ Dead-end detected: {name}, 0/{round_data['failed']} lemmas passed", flush=True)
            break

        if round_idx < max_refine:
            # Refine: adjust DAG for failures
            bp = refine_blueprint(bp, llm_generate=llm_gen)
            result["blueprint_refinements"] += 1
        else:
            result["errors"].append(f"Max refinement rounds ({max_refine}) reached")

    result["total_time_s"] = time.perf_counter() - t_start
    return result


# ── Main ─────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Blueprint Benchmark")
    parser.add_argument("--problems", type=str, default=BENCHMARK_FILE)
    parser.add_argument("--max", type=int, default=MAX_PROBLEMS)
    parser.add_argument("--k", type=int, default=K_PASS)
    parser.add_argument("--refine", type=int, default=MAX_REFINEMENTS)
    parser.add_argument("--tag", type=str, default="blueprint_baseline")
    args = parser.parse_args()

    # ── Load problems ──
    problems = []
    with open(args.problems) as f:
        for line in f:
            d = json.loads(line.strip())
            formal = d.get("formal_statement", "") or d.get("lean4_code", "")
            for line_text in formal.split("\n"):
                line_text = line_text.strip()
                if line_text.startswith("theorem ") or line_text.startswith("lemma "):
                    if ":=" in line_text:
                        line_text = line_text.split(":=")[0].strip() + " :="
                    problems.append(line_text)
                    break
    if args.max:
        problems = problems[:args.max]

    print(f"Blueprint Benchmark: {args.tag}")
    print(f"  Problems: {len(problems)}")
    print(f"  k={args.k}, max_refine={args.refine}")
    print("=" * 60)
    sys.stdout.flush()

    # ── Init ──
    passk_mgr = OmegaPassKManager(backend="deepseek")
    llm_gen = make_llm_generate()

    # ── Run ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ckpt_path = EXPERIMENTS_DIR / f"{args.tag}_{ts}.jsonl"

    results = []
    t_start = time.perf_counter()
    for i, header in enumerate(problems):
        t_prob = time.perf_counter()
        r = run_one_problem(header, passk_mgr, llm_gen, k=args.k, max_refine=args.refine)
        r["index"] = i

        # Per-problem checkpoint
        with open(ckpt_path, "a") as f:
            f.write(json.dumps(r) + "\n")
        results.append(r)

        status = "OK" if r["succeeded"] else "FAIL"
        rounds_info = "/".join(f"{rd['proved']}p{rd['failed']}f" for rd in r["rounds"])
        print(
            f"[{i+1:2d}/{len(problems)}] [{status}] {r['name']:40s} "
            f"lemmas={r['blueprint_lemmas']} "
            f"refines={r['blueprint_refinements']} "
            f"rounds=[{rounds_info}] "
            f"{r['total_time_s']:.1f}s",
            flush=True,
        )

    # ── Summary ──
    total_t = time.perf_counter() - t_start
    proven = sum(1 for r in results if r["succeeded"])
    total_lemmas = sum(r["blueprint_lemmas"] for r in results)
    total_refines = sum(r["blueprint_refinements"] for r in results)

    print()
    print("=" * 60)
    print(f"  {args.tag} RESULTS")
    print("=" * 60)
    print(f"  Problems:       {len(results)}")
    print(f"  Proven:         {proven}/{len(results)} ({100*proven/max(len(results),1):.1f}%)")
    print(f"  Total lemmas:   {total_lemmas}")
    print(f"  Total refines:  {total_refines}")
    print(f"  Total time:     {total_t:.1f}s ({total_t/60:.1f}min)")
    print(f"  Avg time/prob:  {total_t/max(len(results),1):.1f}s")
    print(f"  Checkpoint:     {ckpt_path.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
