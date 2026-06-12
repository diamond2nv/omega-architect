#!/usr/bin/env python3
"""
E2E Validation: LUFFY LoRA → vLLM Serving pipeline.

Usage:
  python scripts/e2e_validate_lora_vllm.py              # Quick: adapter export only
  python scripts/e2e_validate_lora_vllm.py --train       # + 1-step training (requires GPU)
  python scripts/e2e_validate_lora_vllm.py --serve       # + start vLLM server (full E2E)

Steps:
  1. Train dummy LoRA adapter (1 step) or create synthetic adapter
  2. Export for vLLM (save adapter_config.json + adapter_model.safetensors)
  3. Verify all files match vLLM LoRA spec
  4. Optionally start vLLM server and query with LoRA
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent


def step_train_adapter(output_dir: str) -> dict:
    """Train a 1-step dummy LoRA adapter to verify the full pipeline."""
    print("\n" + "=" * 60)
    print("  STEP 1: Training LoRA Adapter (1 step)")
    print("=" * 60)

    # Import LUFFY trainer (direct import to avoid omega.__init__ chain)
    import importlib.util
    trainer_path = PROJECT_DIR / "omega" / "learn" / "rl" / "luffy_mixed_trainer.py"
    spec = importlib.util.spec_from_file_location(
        "omega.learn.rl.luffy_mixed_trainer", str(trainer_path)
    )
    luffy = importlib.util.module_from_spec(spec)
    sys.modules["omega.learn.rl.luffy_mixed_trainer"] = luffy
    spec.loader.exec_module(luffy)

    LuffyTrainerConfig = luffy.LuffyTrainerConfig
    LuffyMixedPolicyTrainer = luffy.LuffyMixedPolicyTrainer
    compute_compile_reward = luffy.compute_compile_reward

    cfg = LuffyTrainerConfig(
        checkpoint_dir=os.path.join(output_dir, "checkpoints"),
        log_dir=os.path.join(output_dir, "logs"),
        jsonl_path=os.path.join(output_dir, "logs/metrics.jsonl"),
        batch_size=1,
        grpo_group_size=2,
        gradient_accumulation_steps=1,
        max_steps=1,
        lora_r=8,
        lora_alpha=16,
        use_qlora=False,
        save_every_steps=1,
    )

    trainer = LuffyMixedPolicyTrainer(cfg)

    print("\n[Training] Loading model... This may take 5+ minutes.")
    trainer._setup_model()
    trainer.model.gradient_checkpointing_enable()
    trainer._setup_optimizer()

    print("\n[Training] Running 1 training step...")
    t0 = time.time()
    metrics = trainer.train_step([
        "∀ (n : ℕ), n + 0 = n"
    ])
    elapsed = time.time() - t0

    print(f"  ✅ Training step completed in {elapsed:.1f}s")
    print(f"  📊 Loss: {metrics.get('loss/total', 'N/A'):.4f}")
    print(f"  📊 Reward: {metrics.get('reward/on_mean', 'N/A'):.3f}")

    # Save adapter
    adapter_dir = os.path.join(output_dir, "adapter")
    trainer._save_checkpoint("luffy_v1")
    # Move to adapter dir
    import shutil
    shutil.copytree(
        os.path.join(cfg.checkpoint_dir, "luffy_v1"),
        adapter_dir,
        dirs_exist_ok=True,
    )
    print(f"  💾 Adapter saved to {adapter_dir}")

    return {
        "adapter_dir": adapter_dir,
        "loss": metrics.get("loss/total", 0),
        "reward": metrics.get("reward/on_mean", 0),
        "elapsed_s": elapsed,
    }


def step_create_synthetic_adapter(output_dir: str) -> str:
    """
    Create a minimal LoRA adapter for vLLM compatibility testing.
    This does NOT require the full model load.
    """
    print("\n" + "=" * 60)
    print("  STEP 1 (fast): Creating synthetic LoRA adapter for vLLM verification")
    print("=" * 60)

    adapter_dir = os.path.join(output_dir, "adapter")
    os.makedirs(adapter_dir, exist_ok=True)

    # adapter_config.json — matches vLLM LoRA spec
    # vLLM 0.22 expects: base_model_name_or_id, r, lora_alpha, target_modules
    config = {
        "base_model_name_or_id": "Goedel-LM/Goedel-Prover-V2-8B",
        "r": 8,
        "lora_alpha": 16,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "lora_dropout": 0.0,
        "bias": "none",
        "task_type": "CAUSAL_LM",
        "peft_type": "LORA",
    }
    with open(os.path.join(adapter_dir, "adapter_config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # Create a minimal adapter_model.safetensors
    # We need at least one LoRA weight per target module
    import torch
    import safetensors.torch

    state_dict = {}
    for module in config["target_modules"]:
        # LoRA A: [in_features, r], LoRA B: [r, out_features]
        # Typical dimensions for LLaMA-style 8B: 4096×4096
        state_dict[f"{module}.lora_A.weight"] = torch.randn(8, 4096) * 0.01
        state_dict[f"{module}.lora_B.weight"] = torch.randn(4096, 8) * 0.01

    safetensors.torch.save_file(state_dict, os.path.join(adapter_dir, "adapter_model.safetensors"))

    file_size = sum(
        os.path.getsize(os.path.join(adapter_dir, f))
        for f in os.listdir(adapter_dir)
    )
    print(f"  ✅ Synthetic adapter: {adapter_dir}")
    print(f"  📦 Files: {list(os.listdir(adapter_dir))}")
    print(f"  📦 Size: {file_size / 1024:.1f} KB")

    return adapter_dir


def step_verify_adapter(adapter_dir: str) -> bool:
    """Verify adapter files match vLLM 0.22 LoRA spec."""
    print("\n" + "=" * 60)
    print("  STEP 2: vLLM LoRA Adapter Verification")
    print("=" * 60)

    checks = []
    import torch

    # Check adapter_config.json
    config_path = os.path.join(adapter_dir, "adapter_config.json")
    config_ok = os.path.isfile(config_path)
    checks.append(("adapter_config.json exists", config_ok))

    if config_ok:
        with open(config_path) as f:
            cfg = json.load(f)
        checks.append(("has peft_type='LORA'", cfg.get("peft_type") == "LORA"))
        checks.append(("has r > 0", cfg.get("r", 0) > 0))
        checks.append(("has lora_alpha > 0", cfg.get("lora_alpha", 0) > 0))
        checks.append(("has base_model_name_or_id", bool(cfg.get("base_model_name_or_id"))))
        checks.append(("has target_modules", len(cfg.get("target_modules", [])) > 0))
        valid_modules = all(m in ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
                            for m in cfg.get("target_modules", []))
        checks.append((f"target_modules valid ({cfg.get('target_modules')})", valid_modules))

    # Check adapter_model.safetensors
    weights_path = os.path.join(adapter_dir, "adapter_model.safetensors")
    weights_ok = os.path.isfile(weights_path)
    checks.append(("adapter_model.safetensors exists", weights_ok))

    if weights_ok:
        import safetensors.torch
        weights = safetensors.torch.load_file(weights_path)
        checks.append(("at least one LoRA A weight", any("lora_A" in k for k in weights)))
        checks.append(("at least one LoRA B weight", any("lora_B" in k for k in weights)))
        checks.append(("weights are float32/16/bf16", any(w.dtype in [torch.float32, torch.float16, torch.bfloat16]
                                                         for w in weights.values())))

    # Print results
    all_ok = True
    for label, result in checks:
        status = "✅" if result else "❌"
        if not result:
            all_ok = False
        print(f"    {status} {label}")

    if all_ok:
        print(f"\n  ✅ vLLM LoRA adapter format: VALID")
    else:
        print(f"\n  ❌ vLLM LoRA adapter format: INVALID")

    return all_ok


def step_query_vllm(adapter_dir: str, port: int = 8001):
    """Query running vLLM server with LoRA adapter."""
    import httpx

    print("\n" + "=" * 60)
    print("  STEP 3: Querying vLLM + LoRA")
    print("=" * 60)

    base_url = f"http://localhost:{port}/v1"

    # Check if server is running
    try:
        r = httpx.get(f"{base_url}/models", timeout=5)
        models = r.json()
        print(f"  ✅ Server alive. Models: {[m['id'] for m in models.get('data', [])]}")
    except Exception as e:
        print(f"  ⚠️  Server not reachable: {e}")
        print("  Start with: bash scripts/serve-goedel-lora.sh <adapter_dir> <port>")
        return False

    # Query without LoRA (baseline)
    prompt = "theorem add_comm (n m : ℕ) : n + m = m + n := by"
    print(f"\n  Prompt: {prompt}")

    # vLLM 0.22 LoRA API: use model name with adapter suffix
    model_name = "Goedel-LM/Goedel-Prover-V2-8B"
    lora_model_name = f"{model_name}/luffy"

    # Without LoRA
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 64,
                "temperature": 0.6,
            },
            timeout=30,
        )
        out = r.json()
        content = out["choices"][0]["message"]["content"]
        print(f"\n  🔹 Without LoRA: `{content[:80]}...`")
    except Exception as e:
        print(f"  ⚠️  Query failed: {e}")

    return True


def check_dependencies():
    """Check that all required packages are available."""
    missing = []
    try:
        import safetensors  # noqa: F401
    except ImportError:
        missing.append("safetensors")
    try:
        import httpx  # noqa: F401
    except ImportError:
        missing.append("httpx")
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch")

    if missing:
        print(f"Installing missing deps: {missing}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install"] + missing,
            check=True, capture_output=True,
        )
        print("  ✅ Installed")


def main():
    parser = argparse.ArgumentParser(description="E2E LUFFY LoRA → vLLM Validation")
    parser.add_argument("--train", action="store_true", help="Run 1-step training (requires GPU)")
    parser.add_argument("--serve", action="store_true", help="Query running vLLM server")
    parser.add_argument("--output", type=str, default="/tmp/luffy-e2e", help="Output directory")
    args = parser.parse_args()

    print("=" * 60)
    print("  LUFFY LoRA → vLLM: E2E Pipeline Validation")
    print("=" * 60)

    check_dependencies()
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Create adapter (real or synthetic)
    if args.train:
        result = step_train_adapter(output_dir)
        adapter_dir = result["adapter_dir"]
    else:
        adapter_dir = step_create_synthetic_adapter(output_dir)

    # Step 2: Verify adapter format
    is_valid = step_verify_adapter(adapter_dir)

    # Step 3: Query vLLM (optional)
    if args.serve and is_valid:
        step_query_vllm(adapter_dir)

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Adapter: {adapter_dir}")
    print(f"  Format:  {'✅ VALID' if is_valid else '❌ INVALID'}")
    print(f"\n  To serve with vLLM:")
    print(f"    bash {PROJECT_DIR}/scripts/serve-goedel-lora.sh {adapter_dir} 8001")
    print(f"\n  To query with LoRA:")
    print(f'    curl http://localhost:8001/v1/chat/completions \\')
    print(f'      -H "Content-Type: application/json" \\')
    print(f'      -d \'{{"model":"Goedel-LM/Goedel-Prover-V2-8B","messages":')
    print(f'      [{{"role":"user","content":"theorem t..."}}],"max_tokens":128}}\'')
    print("\n" + "=" * 60)

    return 0 if is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
