#!/usr/bin/env python3
"""Validate Goedel-Prover-V2-8B: inference speed, VRAM, LoRA trainability.

Usage:
  python scripts/validate_goedel_lora.py           # quick inference only
  python scripts/validate_goedel_lora.py --lora     # + LoRA training validation
"""

import argparse
import time
import json
import sys
import os

os.environ["HF_HOME"] = "/mnt/d/home/.cache"
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

import torch
import transformers
import peft

def print_sep(title: str):
    print(f"\n{'='*72}")
    print(f"  {title}")
    print(f"{'='*72}")

def validate_inference():
    """Step 1: Load model and measure inference speed/VRAM."""
    print_sep("PHASE 1: INFERENCE VALIDATION")

    model_id = "Goedel-LM/Goedel-Prover-V2-8B"
    print(f"Model: {model_id}")
    print(f"PyTorch: {torch.__version__} | CUDA: {torch.cuda.is_available()}")

    # Cache check
    cache_path = os.path.expanduser("~/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B")
    if os.path.isdir(cache_path):
        print(f"✅ Model cached locally: {cache_path}")
    else:
        print(f"⚠️  Model not found in cache, will download from HF")

    # ── Load tokenizer ──
    print("\n[1/4] Loading tokenizer...")
    t0 = time.time()
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_id, trust_remote_code=True,
        cache_dir=os.environ.get("HF_HOME") + "/hub"
    )
    print(f"  ✅ Loaded in {time.time()-t0:.1f}s | vocab_size={tokenizer.vocab_size}")

    # ── Load model (FP16) ──
    print("\n[2/4] Loading model (bfloat16)...")
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
        cache_dir=os.environ.get("HF_HOME") + "/hub",
    )
    load_time = time.time() - t0
    base_vram = torch.cuda.memory_allocated() / 1e9
    max_vram = torch.cuda.max_memory_allocated() / 1e9
    print(f"  ✅ Loaded in {load_time:.1f}s")
    print(f"  📊 VRAM: {base_vram:.2f} GB allocated | {max_vram:.2f} GB peak")
    print(f"  📊 Params: {sum(p.numel() for p in model.parameters())/1e9:.1f}B")
    print(f"  📊 Dtype: {model.dtype}")

    # ── Warmup ──
    print("\n[3/4] Warmup inference...")
    prompt = "theorem add_comm (n m : ℕ) : n + m = m + n := by"
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    with torch.no_grad():
        _ = model.generate(
            **inputs,
            max_new_tokens=32,
            do_sample=True,
            temperature=0.6,
            top_p=0.9,
        )

    # ── Benchmark inference ──
    print("\n[4/4] Benchmarking inference (5 runs)...")
    prompt_len = inputs["input_ids"].shape[1]
    gen_tokens = 128
    latencies = []
    tokens_per_sec = []
    torch.cuda.reset_peak_memory_stats()

    for i in range(5):
        t0 = time.time()
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=gen_tokens,
                do_sample=True,
                temperature=0.6,
                top_p=0.9,
                pad_token_id=tokenizer.eos_token_id,
            )
        elapsed = time.time() - t0
        new_tokens = out.shape[1] - prompt_len
        latencies.append(elapsed)
        tokens_per_sec.append(new_tokens / elapsed)
        print(f"  Run {i+1}: {elapsed:.2f}s | {new_tokens} tokens | {new_tokens/elapsed:.1f} tok/s")

    avg_tps = sum(tokens_per_sec) / len(tokens_per_sec)
    inf_vram = torch.cuda.max_memory_allocated() / 1e9
    print(f"\n  📊 Average: {avg_tps:.1f} tok/s | Inference VRAM peak: {inf_vram:.2f} GB")

    return model, tokenizer, {
        "load_time_s": load_time,
        "base_vram_gb": base_vram,
        "max_vram_gb": max_vram,
        "inference_vram_peak_gb": inf_vram,
        "avg_tps": avg_tps,
        "params_b": sum(p.numel() for p in model.parameters()) / 1e9,
    }

def validate_lora(model, tokenizer, base_stats: dict):
    """Step 2: Validate LoRA training with PEFT."""
    print_sep("PHASE 2: LoRA TRAINING VALIDATION")

    torch.cuda.reset_peak_memory_stats()
    torch.cuda.empty_cache()

    # ── LoRA config ──
    print("\n[1/5] Configuring LoRA...")
    lora_config = peft.LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.1,
        bias="none",
        task_type="CAUSAL_LM",
    )
    print(f"  r={lora_config.r}, alpha={lora_config.lora_alpha}")
    print(f"  targets={lora_config.target_modules}")

    # ── Wrap with LoRA ──
    print("\n[2/5] Wrapping model with LoRA...")
    t0 = time.time()
    lora_model = peft.get_peft_model(model, lora_config)
    lora_model.print_trainable_parameters()
    lora_model = lora_model.to("cuda")
    lora_model.train()
    lora_vram = torch.cuda.memory_allocated() / 1e9
    print(f"  LoRA wrap time: {time.time()-t0:.1f}s")
    print(f"  📊 LoRA VRAM: {lora_vram:.2f} GB allocated")
    print(f"  📊 LoRA params: {sum(p.numel() for p in lora_model.parameters() if p.requires_grad)/1e3:.0f}K trainable")

    # ── Enable gradient checkpointing ──
    print("\n[3/5] Enabling gradient checkpointing...")
    lora_model.gradient_checkpointing_enable()
    print("  ✅ Gradient checkpointing enabled")

    # ── Test forward + backward ──
    print("\n[4/5] Testing forward + backward pass...")
    prompt = "theorem add_comm (n m : ℕ) : n + m = m + n := by\n  induction n with\n  | zero =>\n    simp\n  | succ n ih =>\n"
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    labels = inputs["input_ids"].clone()

    t0 = time.time()
    outputs = lora_model(**inputs, labels=labels)
    loss = outputs.loss
    loss.backward()
    backward_time = time.time() - t0
    train_vram = torch.cuda.max_memory_allocated() / 1e9
    print(f"  Loss: {loss.item():.4f}")
    print(f"  Forward+backward: {backward_time:.2f}s")
    print(f"  📊 Training VRAM peak: {train_vram:.2f} GB")
    print(f"  📊 Gradient norm: {torch.nn.utils.clip_grad_norm_(lora_model.parameters(), 1.0):.4f}")

    # ── Gradient accumulation estimate ──
    print("\n[5/5] Estimating batch_size limits...")
    prompt_len = inputs["input_ids"].shape[1]
    for bs in [2, 4, 8]:
        est_vram = train_vram * (1 + 0.15 * (bs - 1))
        feasible = est_vram < 21  # leave 1.5GB headroom from 22.5GB usable
        print(f"  batch_size={bs}: ~{est_vram:.1f} GB {'✅' if feasible else '❌'}")

    # Cleanup
    del lora_model
    torch.cuda.empty_cache()

    return {
        "lora_vram_gb": lora_vram,
        "train_vram_peak_gb": train_vram,
        "backward_time_s": backward_time,
        "lora_params_k": sum(p.numel() for p in lora_model.parameters() if p.requires_grad) / 1e3,
    }

def check_vllm_lora_support():
    """Check if installed vLLM supports LoRA serving."""
    print_sep("PHASE 3: vLLM + LoRA COMPATIBILITY CHECK")
    try:
        import vllm
        print(f"vLLM version: {vllm.__version__}")

        # Check LoRA support
        from vllm.config import LoRAConfig
        print(f"  ✅ LoRA config class available")
        print(f"  ✅ vLLM version {vllm.__version__} supports LoRA serving")

        # Check quantization support
        print(f"  Available quantization methods:")
        print(f"    - AWQ: needs `autoawq`")
        print(f"    - GPTQ: needs `auto-gptq`")
        print(f"    - bitsandbytes: needs `bitsandbytes`")
        print(f"    - FP8: native support in vLLM ≥0.6")

    except ImportError:
        print("⚠️  vLLM not installed or importable")
    except AttributeError:
        print("⚠️  This vLLM version may not support LoRA (need v0.4.0+)")


def main():
    parser = argparse.ArgumentParser(description="Validate Goedel-Prover-V2-8B")
    parser.add_argument("--lora", action="store_true", help="Also validate LoRA training")
    parser.add_argument("--output", type=str, default=None, help="Save results to JSON")
    args = parser.parse_args()

    print("═" * 72)
    print("  Goedel-Prover-V2-8B Validation Suite")
    print(f"  GPU: {torch.cuda.get_device_name(0)}")
    print(f"  VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    print(f"  Transformers: {transformers.__version__} | PEFT: {peft.__version__}")
    print("═" * 72)

    # Phase 1: Inference
    model, tokenizer, base_stats = validate_inference()

    # Phase 3: vLLM check (independent)
    check_vllm_lora_support()

    # Phase 2: LoRA (optional)
    lora_stats = {}
    if args.lora:
        lora_stats = validate_lora(model, tokenizer, base_stats)

    # Cleanup
    del model
    torch.cuda.empty_cache()

    # Summary
    print_sep("VALIDATION SUMMARY")
    summary = {
        "device": torch.cuda.get_device_name(0),
        "vram_total_gb": torch.cuda.get_device_properties(0).total_memory / 1e9,
        "inference": base_stats,
        "lora": lora_stats,
        "recommendations": [],
    }

    if base_stats["avg_tps"] > 20:
        print("✅ Inference: FAST")
    elif base_stats["avg_tps"] > 10:
        print("⚠️  Inference: MODERATE")
    else:
        print("❌ Inference: SLOW")

    if lora_stats:
        vram_left = 24.0 - lora_stats.get("train_vram_peak_gb", 24)
        print(f"✅ LoRA training: {'FEASIBLE' if vram_left > 2 else 'TIGHT'}")
        print(f"   Available VRAM after training: {24.0 - lora_stats.get('train_vram_peak_gb', 24):.1f} GB")
        print(f"   Recommended: batch_size=1, grad_accum=4, QLoRA for more headroom")

    # Recommendations
    if lora_stats and lora_stats.get("train_vram_peak_gb", 0) > 20:
        summary["recommendations"].append("QLoRA (bitsandbytes 4-bit) reduces base VRAM to ~5GB, enabling batch_size≥4")
    summary["recommendations"].append("After LoRA training: merge adapter OR serve via vLLM --enable-lora --lora-modules")
    summary["recommendations"].append("vLLM supports multiple LoRA adapters simultaneously → useful for 5-domain specialization")
    summary["recommendations"].append("AWQ quantization can be applied after merging LoRA for further VRAM reduction")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\n📄 Results saved to {args.output}")

    print("\n" + "═" * 72)
    return summary


if __name__ == "__main__":
    main()
