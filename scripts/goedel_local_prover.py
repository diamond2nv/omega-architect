#!/usr/bin/env python3
"""Goedel-Prover-V2 local vLLM inference — matching official prompt format.

Based on Goedel-Prover-V2's src/utils.py (DeepSeekCoTHandler):
  https://github.com/Goedel-LM/Goedel-Prover-V2/blob/main/src/utils.py

Key differences from v1 (broken):
  1) Use tokenizer.apply_chat_template (model-native chat format)
  2) Prompt target: `:= by sorry`  (not bare `:=`)
  3) Extract LAST ```lean4 block  (model outputs CoT + code)
  4) replace_statement_in_proof: stitch original header + model proof body
  5) Prepend import block to extracted code

Usage:
  echo '{"theorem_header": "theorem mathd_algebra_148 (x : ℝ) : x * (-2) + 8 = x :=", "k_local": 8, "idx": 0}' | \
    /home/shenli/miniconda3/bin/python scripts/goedel_local_prover.py

Output: JSONL per line.
  lean_code = complete Lean 4 proof (pre-stitched with header + imports).
"""

import json, sys, os, time, re
os.environ["VLLM_USE_V1"] = "1"

MODEL_PATH = "/home/shenli/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/snapshots/dfd02e6271a58375dfbf3ece0175277cf6b6a89a"

IMPORT_BLOCK = "import Mathlib\nimport Aesop\n\nset_option maxHeartbeats 0\n\nopen BigOperators Real Nat Topology Rat\n\n"


# ── Official Goedel utilities (from src/utils.py) ──────────────

def remove_comments(text: str) -> str:
    """Remove Lean comments: /- ... -/ and -- line comments."""
    text = re.sub(r'/-.*?-/', '', text, flags=re.DOTALL)
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        cleaned_line = line.split('--', 1)[0]
        cleaned_lines.append(cleaned_line)
    return '\n'.join(cleaned_lines).strip()


def return_theorem_to_prove(text: str):
    """Find the theorem/lemma header up to `:= by sorry`."""
    pattern = r'((?:theorem).*?:=\s*by\s*sorry)'
    match = re.search(pattern, text, re.DOTALL)
    return match.span() if match else None


def return_theorem_to_replace(text: str):
    """Find the theorem/lemma header up to `:= by`."""
    pattern = r'((?:^|\s)theorem\s+.*?:=\s*by)'
    match = re.search(pattern, text, re.DOTALL)
    return match.span() if match else None


def replace_statement_in_proof(statement: str, proof: str) -> str:
    """Official Goedel: stitch original statement header + model proof body."""
    stats_re = remove_comments(statement)
    stats_span_ = return_theorem_to_prove(stats_re)
    if stats_span_ is None:
        return f"**Error**, cannot find 'theorem' and ':= sorry' in statement."
    proof_str = remove_comments(proof)
    span = return_theorem_to_replace(proof_str)
    if span is None:
        return f"**Error**, cannot find 'theorem' and ':=' in proof."
    return stats_re[:stats_span_[1]].replace("sorry", "") + proof_str[span[1]:]


# ── Prompt building (matching DeepSeekCoTHandler) ──────────────

def build_formal_statement(header: str) -> str:
    """Convert theorem header to `:= by sorry` format."""
    # header is like: "theorem foo (x : ℝ) : x = x :="
    # Need: "theorem foo (x : ℝ) : x = x := by sorry"
    # If header ends with :=, append " by sorry"
    h = header.strip()
    if h.endswith(":="):
        return h + " by sorry"
    elif ":=" not in h:
        return h + " := by sorry"
    else:
        # Header already has :=, insert by sorry after it
        parts = h.split(":=", 1)
        return parts[0] + ":= by sorry" + parts[1]


def build_prompt(header: str) -> str:
    """Build chat messages using Goedel's official prompt format.

    Matches DeepSeekCoTHandler.prover_inference():
      "Complete the following Lean 4 code:\n\n```lean4\n{formal_statement}```\n\n
       Before producing the Lean 4 code to formally prove the given theorem,
       provide a detailed proof plan outlining the main proof steps..."

    The model outputs CoT reasoning + code in ```lean4 blocks.
    The LAST ```lean4 block is the final proof.
    """
    formal_statement = build_formal_statement(header)
    prompt = (
        f"Complete the following Lean 4 code:\n\n"
        f"```lean4\n{formal_statement}```\n\n"
        f"Before producing the Lean 4 code to formally prove the given theorem, "
        f"provide a detailed proof plan outlining the main proof steps and strategies. "
        f"The plan should highlight key ideas, intermediate lemmas, and proof structures "
        f"that will guide the construction of the final formal proof."
    )
    messages = [{"role": "user", "content": prompt}]
    return messages


# ── Code extraction (matching DeepSeekCoTHandler.extrac_code) ──

def extrac_code_official(raw_text: str) -> str:
    """Official Goedel code extraction.

    Matches DeepSeekCoTHandler.extrac_code:
      - Take the LAST ```lean4 block
      - Prepend import block
    """
    # Try ```lean4 ...
    pattern = r'```lean4\n(.*?)\n```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return IMPORT_BLOCK + matches[-1]

    # Try ```lean4 ... (without trailing newline)
    pattern = r'```lean4\n(.*?)```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return IMPORT_BLOCK + matches[-1]

    # Try ```lean ...
    pattern = r'```lean\n(.*?)```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return IMPORT_BLOCK + matches[-1]

    return "None"


# ── API Client (remote server mode) ─────────────────────────────

def call_vllm_api(messages: list[dict], k: int = 1,
                  api_url: str = "http://localhost:8001/v1/chat/completions",
                  api_key: str = "goe@local",
                  model_id: str = MODEL_PATH,
                  temperature: float = 0.6,
                  max_tokens: int = 2048) -> list[str]:
    """Call running vLLM server via OpenAI-compatible API."""
    import requests
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.95,
        "n": 1,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    texts = []
    for _ in range(k):
        resp = requests.post(api_url, json=payload, headers=headers, timeout=300)
        resp.raise_for_status()
        texts.append(resp.json()["choices"][0]["message"]["content"])
    return texts


def check_server_alive(api_url: str = "http://localhost:8001/health") -> bool:
    import requests
    try:
        r = requests.get(api_url, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


# ── Main inference loop (via GPU Layer) ────────────────────────

def main():
    lines = [l.strip() for l in sys.stdin if l.strip()]
    problems = [json.loads(l) for l in lines]

    from omega.gpu_layer import gpu_scheduler

    print(f"[goedel_prover] Using GPU Layer scheduler (vLLM server :8001 or auto-start)",
          file=sys.stderr, flush=True)

    for prob in problems:
        header = prob["theorem_header"]
        k = prob.get("k_local", 8)
        idx = prob.get("idx", 0)

        # Build messages
        messages = build_prompt(header)

        # Generate via GPU Layer (auto-routes to vLLM server, starts if needed)
        try:
            texts = gpu_scheduler.generate(
                messages=messages,
                model="goedel",
                max_tokens=2048,
                temperature=0.6,
                n=k,
            )
        except Exception as e:
            print(f"[goedel_prover] gpu_scheduler failed: {e}",
                  file=sys.stderr, flush=True)
            continue

        seen_codes = set()
        for raw in texts:
            # Step 1: Extract code from ```lean4 blocks
            full_code = extrac_code_official(raw)
            if full_code == "None":
                continue

            # Step 2: Stitch original statement header + model proof body
            formal_statement = build_formal_statement(header)
            stitched = replace_statement_in_proof(formal_statement, full_code)

            if stitched.startswith("**Error**") or len(stitched) < 20:
                continue

            # Extract proof body for benchmark
            body = extract_proof_body(stitched)

            if not body or len(body) < 5:
                continue

            if body in seen_codes:
                continue
            seen_codes.add(body)

            result = {
                "lean_code": body,
                "confidence": 0.5,
                "strategy": "goedel_local",
                "model": "Goedel-Prover-V2-8B",
                "idx": idx,
            }
            print(json.dumps(result), flush=True)


def extract_proof_body(full_code: str) -> str:
    """Strip imports, options, opens, and theorem header. Keep only proof body.

    Input:
      import Mathlib
      import Aesop
      set_option maxHeartbeats 0
      open BigOperators Real Nat Topology Rat

      theorem mathd_algebra_148 (x : ℝ) : x * (-2) + 8 = x := by
        nlinarith

    Output:
      by
        nlinarith
    """
    # Remove import/set_option/open lines
    lines = full_code.split('\n')
    filtered = []
    for line in lines:
        stripped = line.strip()
        if (stripped.startswith('import ') or
            stripped.startswith('set_option ') or
            stripped.startswith('open ')):
            continue
        filtered.append(line)

    code = '\n'.join(filtered).strip()

    # Remove theorem/lemma header — find `:=` and keep everything after
    # Handle: `theorem foo ... :=` or `theorem foo ... := by`
    # Keep just the proof (after the `:=`)
    idx = code.find(':=')
    if idx >= 0:
        after_colon_eq = code[idx + 2:].strip()
        # If it's `:= by ...`, return `by ...`
        # If it's `:= ...`, return `...`
        return after_colon_eq

    return code


if __name__ == "__main__":
    main()
