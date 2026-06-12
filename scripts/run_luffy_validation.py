#!/usr/bin/env python3
"""
MiniF2F Validation Training — LUFFY QLoRA, 10 steps, batch_size=4.

Usage:
  bash scripts/run_luffy_validation.sh

Logs:  ./logs/luffy_val/
Model: ./checkpoints/luffy_val/
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent

# Direct import (bypass omega.__init__ chain)
luffy_path = PROJECT_DIR / "omega" / "learn" / "rl" / "luffy_mixed_trainer.py"
spec = importlib.util.spec_from_file_location("omega.learn.rl.luffy_mixed_trainer", str(luffy_path))
luffy = importlib.util.module_from_spec(spec)
sys.modules["omega.learn.rl.luffy_mixed_trainer"] = luffy
spec.loader.exec_module(luffy)

LuffyTrainerConfig = luffy.LuffyTrainerConfig
LuffyMixedPolicyTrainer = luffy.LuffyMixedPolicyTrainer


def load_theorems(path: str, max_n: int = 20) -> list[dict]:
    """Load theorems from MiniF2F JSONL."""
    theorems = []
    with open(path) as f:
        for line in f:
            if line.strip():
                theorems.append(json.loads(line))
    if max_n:
        theorems = theorems[:max_n]
    return theorems


def format_theorem_for_training(t: dict) -> str:
    """Extract the formal statement as a training prompt."""
    return t.get("formal_statement", "").strip()


def main():
    print("=" * 60)
    print("  LUFFY QLoRA Validation Training (MiniF2F)")
    print("=" * 60)

    # ── Data ──
    data_path = "/tmp/minif2f_10.jsonl"
    if not os.path.isfile(data_path):
        print(f"❌ Data not found: {data_path}")
        sys.exit(1)

    theorems = load_theorems(data_path, max_n=8)
    statements = [format_theorem_for_training(t) for t in theorems]
    print(f"  📚 Loaded {len(statements)} theorems")
    for t in theorems[:3]:
        print(f"     - {t['name']}")

    train_set = statements[:4]
    eval_set = statements[4:8]
    print(f"  Train: {len(train_set)} | Eval: {len(eval_set)}")

    # ── Config ──
    cfg = LuffyTrainerConfig(
        base_model_id="Goedel-LM/Goedel-Prover-V2-8B",
        hf_cache_dir="/mnt/d/home/.cache/hub",
        checkpoint_dir="./checkpoints/luffy_leak_test",
        log_dir="./logs/luffy_leak_test",
        jsonl_path="./logs/luffy_leak_test/metrics.jsonl",
        batch_size=2,
        grpo_group_size=4,
        gradient_accumulation_steps=2,
        max_steps=10,
        lora_r=8,
        lora_alpha=16,
        lora_dropout=0.05,
        use_qlora=True,
        qlora_bits=4,
        learning_rate=2e-5,
        warmup_steps=2,
        save_every_steps=5,
        log_every_steps=1,
        policy_shaping_gamma=3.0,
        lambda_off=0.5,
        kl_beta=0.01,
        grpo_temperature=0.6,
        grpo_max_new_tokens=256,
    )

    print(f"\n  Config:")
    print(f"    LoRA: r={cfg.lora_r}, alpha={cfg.lora_alpha}, QLoRA={cfg.use_qlora}")
    print(f"    Batch: {cfg.batch_size} × {cfg.grpo_group_size} = {cfg.batch_size * cfg.grpo_group_size} candidates/step")
    print(f"    Steps: {cfg.max_steps} | Grad accum: {cfg.gradient_accumulation_steps}")
    print(f"    Checkpoints: {cfg.checkpoint_dir}")

    # ── Trainer ──
    print("\n  Initializing trainer...")
    trainer = LuffyMixedPolicyTrainer(cfg)

    # ── Training ──
    print("\n" + "=" * 60)
    print("  TRAINING")
    print("=" * 60)

    trainer._setup_model()
    print(f"\n  Base VRAM: {torch.cuda.memory_allocated() / 1e9:.2f} GB")

    trainer.model.gradient_checkpointing_enable()
    trainer._setup_optimizer()

    print(f"\n  {'Step':>6s} | {'Loss':>8s} | {'R_on':>6s} | {'LR':>10s} | {'Time':>8s} | {'VRAM':>8s}")
    print(f"  {'-'*52}")

    global_step = 0
    n_batches = max(1, len(train_set) // cfg.batch_size)

    for step in range(cfg.max_steps):
        step_start = time.time()
        batch_start = (step % n_batches) * cfg.batch_size
        batch_end = min(batch_start + cfg.batch_size, len(train_set))
        batch = train_set[batch_start:batch_end]

        metrics = trainer.train_step(batch)

        elapsed = time.time() - step_start
        loss = metrics.get("loss/total", 0)
        r_on = metrics.get("reward/on_mean", 0)
        lr = metrics.get("lr", 0)
        vram = metrics.get("vram/allocated_gb", 0)

        print(f"  {step:>6d} | {loss:>8.4f} | {r_on:>6.3f} | {lr:>10.2e} | {elapsed:>7.1f}s | {vram:>7.2f}GB")

        # Log
        trainer.logger.log(step, metrics)

        # Checkpoint
        if step > 0 and step % cfg.save_every_steps == 0:
            trainer._save_checkpoint(f"step_{step}")

        global_step += 1

    # ── Final ──
    trainer._save_checkpoint("final")
    trainer.export_for_vllm("./checkpoints/luffy_leak_test/final",
                            output_dir="./checkpoints/luffy_leak_test/vllm_ready",
                            adapter_name="luffy_leak_test")

    print(f"\n{'='*60}")
    print(f"  ✅ Training Complete!")
    print(f"  Checkpoints: {cfg.checkpoint_dir}")
    print(f"  Logs:        {cfg.log_dir}")
    print(f"  vLLM ready:  {cfg.checkpoint_dir}/vllm_ready")
    print(f"{'='*60}")


if __name__ == "__main__":
    import torch
    main()
