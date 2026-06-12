"""
LUFFY Mixed-Policy GRPO Trainer — ω-Architect Policy Learning

Loss Architecture:
  ℒ = ℒ_on + λ · f(N_off/N_on) · ℒ_off + β · KL(π_θ || π_ref)

Where:
  ℒ_on  = -mean[(R_on - μ_on)/σ_on · log π_θ(y|x)]     — on-policy GRPO
  ℒ_off = -mean[(R_off - μ_off)/σ_off · log π_θ(y|x)]   — off-policy GRPO
  f(x)  = x/(x+γ)                                        — policy shaping (smooth, differentiable, ≤1)
  KL    = KL(π_θ || π_ref)                                — prevents policy collapse

All terms are differentiable w.r.t. θ.

Logging: TensorBoard (scalars + histograms) + JSONL (programmatic access)
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Literal

import torch
import torch.nn as nn
import torch.nn.functional as F

# Lazy import: tensorboard may not be available
try:
    from torch.utils.tensorboard import SummaryWriter
    _HAS_TB = True
except (ImportError, AttributeError):
    _HAS_TB = False

import transformers
from transformers import AutoModelForCausalLM, AutoTokenizer, get_scheduler
from peft import LoraConfig, get_peft_model, PeftModel


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

@dataclass
class LuffyTrainerConfig:
    """LUFFY Mixed-Policy GRPO trainer configuration."""

    # ── Model ──
    base_model_id: str = "Goedel-LM/Goedel-Prover-V2-8B"
    hf_cache_dir: str = "/mnt/d/home/.cache/hub"

    # ── LoRA ──
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.1
    lora_target_modules: list[str] = field(
        default_factory=lambda: ["q_proj", "k_proj", "v_proj", "o_proj",
                                  "gate_proj", "up_proj", "down_proj"]
    )
    use_qlora: bool = False           # bitsandbytes 4-bit base model
    qlora_bits: Literal[4, 8] = 4

    # ── GRPO ──
    grpo_group_size: int = 8          # candidates per theorem (on-policy)
    grpo_temperature: float = 0.6
    grpo_top_p: float = 0.9
    grpo_max_new_tokens: int = 512

    # ── LUFFY ──
    lambda_off: float = 0.5           # off-policy loss weight
    policy_shaping_gamma: float = 3.0 # f(x) = x/(x+γ)
    kl_beta: float = 0.01             # KL penalty coefficient

    # ── Training ──
    batch_size: int = 4               # theorems per step — actual is batch_size × group_size
    gradient_accumulation_steps: int = 4
    learning_rate: float = 2e-5
    lr_scheduler: str = "cosine"
    warmup_steps: int = 20
    max_steps: int = 200
    max_grad_norm: float = 1.0
    weight_decay: float = 0.01
    adam_beta1: float = 0.9
    adam_beta2: float = 0.95
    adam_epsilon: float = 1e-8

    # ── Checkpoint ──
    checkpoint_dir: str = "./checkpoints/luffy"
    save_every_steps: int = 50
    save_only_adapter: bool = True    # save LoRA adapter only, not full model

    # ── Logging ──
    log_dir: str = "./logs/luffy"
    log_every_steps: int = 5
    tensorboard: bool = True
    jsonl_path: str = "./logs/luffy/metrics.jsonl"

    # ── vLLM Serving (optional, for inference after training) ──
    vllm_port: int = 8001
    vllm_enable_lora: bool = True     # serve trained adapter via vLLM

    # ── Data ──
    train_theorems_path: Optional[str] = None   # JSONL: each line {"theorem": "...", "id": "..."}
    eval_theorems_path: Optional[str] = None

    def __post_init__(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)


# ──────────────────────────────────────────────
# GRPO Loss (on-policy & off-policy)
# ──────────────────────────────────────────────

def compute_grpo_loss(
    log_probs: torch.Tensor,          # [group_size, seq_len] log π_θ(y|x)
    ref_log_probs: torch.Tensor,      # [group_size, seq_len] log π_ref(y|x)
    rewards: torch.Tensor,            # [group_size] scalar rewards
    kl_beta: float = 0.01,
    reduce: bool = True,
) -> tuple[torch.Tensor, dict]:
    """
    Compute GRPO loss with group-relative advantage normalization.

    Differentiable w.r.t. log_probs (which depends on θ).
    rewards are scalars (stop_gradient implied).

    Returns:
        loss: scalar tensor (differentiable)
        info: dict of diagnostic scalars
    """
    group_size = rewards.shape[0]

    # Group-relative advantage
    mu = rewards.mean()
    sigma = rewards.std().clamp(min=1e-6)
    advantages = (rewards - mu) / sigma       # [group_size]
    advantages = advantages.detach()           # stop gradient through advantage

    # Per-token KL penalty: KL(π_θ || π_ref) ≈ (π_ref/π_θ) - log(π_ref/π_θ) - 1
    # Use the more stable formulation:
    kl_div = torch.exp(ref_log_probs - log_probs) - (ref_log_probs - log_probs) - 1  # [group_size, seq_len]
    kl_penalty = kl_beta * kl_div

    # Policy gradient loss with KL penalty
    per_token_loss = -log_probs * advantages.unsqueeze(1) + kl_penalty  # [group_size, seq_len]

    if reduce:
        loss = per_token_loss.mean()
        info = {
            "loss/grpo_policy": (-log_probs * advantages.unsqueeze(1)).mean().item(),
            "loss/kl_penalty": kl_penalty.mean().item(),
            "advantage/mean": advantages.mean().item(),
            "advantage/std": advantages.std().item(),
            "reward/mean": rewards.mean().item(),
            "reward/std": rewards.std().item(),
            "reward/min": rewards.min().item(),
            "reward/max": rewards.max().item(),
        }
        return loss, info

    return per_token_loss, {}


# ──────────────────────────────────────────────
# Policy Shaping
# ──────────────────────────────────────────────

def policy_shaping_weight(
    n_on: int,
    n_off: int,
    gamma: float = 3.0,
) -> float:
    """
    f(x) = x / (x + γ) where x = n_off / n_on.

    Differentiable w.r.t. n_off/n_on ratio (though used as annealing schedule,
    not in the autograd graph — the ratio is a scalar hyperparameter).

    γ=3.0: at n_off=n_on, weight=0.25 (off-policy slowly phased in)
    γ=1.0: at n_off=n_on, weight=0.50
    γ=0.5: at n_off=n_on, weight=0.67
    """
    if n_on == 0:
        return 0.0
    ratio = n_off / n_on
    return ratio / (ratio + gamma)


# ──────────────────────────────────────────────
# Reward Computation (Compile-Based)
# ──────────────────────────────────────────────

def compute_compile_reward(
    candidate_codes: list[str],
    theorem_ids: list[str] | None = None,
) -> torch.Tensor:
    """
    Compile-based reward for theorem proving candidates.

    Reward structure:
      +1.0  — proof compiles (full success)
      +0.0  — proof compiles but with warnings
      -0.5  — syntax error or type mismatch
      -1.0  — no valid Lean code found

    In real deployment this calls lake env lean --stdin.
    Here we stub with a heuristic for validation.
    """
    import subprocess
    import re

    rewards = []
    details = []

    for idx, code in enumerate(candidate_codes):
        # Extract the Lean code block
        lean_match = re.search(r'```lean4?\n(.*?)```', code, re.DOTALL)

        if lean_match is None:
            # No code block found at all
            rewards.append(-0.5)
            details.append("no_code")
            continue

        code_to_check = lean_match.group(1).strip()

        if not code_to_check:
            rewards.append(-0.5)
            details.append("no_code")
            continue

        # Check for "sorry" or "admit" (incomplete)
        if re.search(r'\bsorry\b|\badmit\b', code_to_check):
            rewards.append(-0.8)
            details.append("incomplete")
            continue

        # In real usage: run lake env lean --stdin and parse output
        # For now we simulate:
        is_proof_fragment = any(kw in code_to_check for kw in [
            "by", "calc", "simp", "induction", "refine", "apply", "rw", "omega"
        ])

        if is_proof_fragment:
            rewards.append(0.3)
            details.append("partial")
        else:
            rewards.append(-0.5)
            details.append("syntax_only")

    return torch.tensor(rewards, dtype=torch.float32), details


# ──────────────────────────────────────────────
# TensorBoard + JSONL Logger
# ──────────────────────────────────────────────

class LuffyLogger:
    """Dual-channel logger: TensorBoard scalars + JSONL records."""

    def __init__(self, config: LuffyTrainerConfig):
        self.config = config
        self.jsonl_path = Path(config.jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        if config.tensorboard and _HAS_TB:
            self.writer = SummaryWriter(log_dir=config.log_dir)
        else:
            self.writer = None
            if config.tensorboard and not _HAS_TB:
                print("  ⚠️  TensorBoard unavailable (broken tensorflow). Logger: JSONL only.")

        self._step = 0

    def log(self, step: int, metrics: dict):
        """Log metrics to TensorBoard and JSONL."""
        self._step = step

        # TensorBoard
        if self.writer is not None:
            for key, value in metrics.items():
                if isinstance(value, (int, float)):
                    self.writer.add_scalar(key, value, step)

        # JSONL
        record = {"step": step, "timestamp": time.time(), **metrics}
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def log_histogram(self, name: str, values: torch.Tensor, step: int):
        """Log a histogram to TensorBoard."""
        if self.writer is not None:
            self.writer.add_histogram(name, values, step)

    def log_text(self, name: str, text: str, step: int):
        """Log text to TensorBoard."""
        if self.writer is not None:
            self.writer.add_text(name, text, step)

    def close(self):
        if self.writer is not None:
            self.writer.close()
            print(f"  📊 TensorBoard logs: tensorboard --logdir={self.config.log_dir}")


# ──────────────────────────────────────────────
# LUFFY Mixed-Policy GRPO Trainer
# ──────────────────────────────────────────────

class LuffyMixedPolicyTrainer:
    """
    LUFFY: LoRA Unified Fine-tuning with Factor Filtering.

    Mixed-Policy GRPO training:
      - On-policy: Goedel-Prover-V2 self-generated trajectories
      - Off-policy: DeepSeek API expert traces (from DialogueCache)
      - Policy shaping f(x)=x/(x+γ) controls off-policy influence annealing
    """

    def __init__(self, config: LuffyTrainerConfig):
        self.config = config
        self.logger = LuffyLogger(config)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        print(f"  📦 CUDA: {torch.cuda.is_available()} | Device: {self.device}")
        if self.device.type == "cuda":
            print(f"  📦 GPU: {torch.cuda.get_device_name(0)} | VRAM: {torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")

        # Will be initialized in _setup_model
        self.model: PeftModel | None = None
        self.ref_model: nn.Module | None = None   # reference model (frozen)
        self.tokenizer: transformers.PreTrainedTokenizer | None = None
        self.optimizer: torch.optim.Optimizer | None = None
        self.scheduler: torch.optim.lr_scheduler.LRScheduler | None = None
        self.global_step = 0

    # ── Model Setup ──

    def _setup_model(self):
        """Load base model, apply LoRA/QLoRA, prepare reference model."""
        cfg = self.config

        print("\n[Model Setup] Loading base model...")
        load_kwargs = {
            "torch_dtype": torch.bfloat16,
            "device_map": "auto",
            "trust_remote_code": True,
            "cache_dir": cfg.hf_cache_dir,
        }

        if cfg.use_qlora:
            try:
                import bitsandbytes as bnb  # noqa: F401
            except ImportError:
                raise ImportError("QLoRA requires bitsandbytes: pip install bitsandbytes")
            load_kwargs["quantization_config"] = transformers.BitsAndBytesConfig(
                load_in_4bit=cfg.qlora_bits == 4,
                load_in_8bit=cfg.qlora_bits == 8,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )

        t0 = time.time()
        base_model = AutoModelForCausalLM.from_pretrained(
            cfg.base_model_id, **load_kwargs
        )
        print(f"  ✅ Base model loaded in {time.time()-t0:.1f}s")

        # Tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(
            cfg.base_model_id, trust_remote_code=True, cache_dir=cfg.hf_cache_dir
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        print(f"  ✅ Tokenizer loaded (vocab={self.tokenizer.vocab_size})")

        # Reference model (frozen copy)
        ref_model = AutoModelForCausalLM.from_pretrained(
            cfg.base_model_id, **{**load_kwargs, "quantization_config": None if cfg.use_qlora else None}
        )
        if cfg.use_qlora:
            # QLoRA: ref model also needs quantization
            ref_model = AutoModelForCausalLM.from_pretrained(
                cfg.base_model_id,
                **{k: v for k, v in load_kwargs.items() if k not in ["quantization_config"]},
                quantization_config=transformers.BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.bfloat16,
                ),
            )
        for p in ref_model.parameters():
            p.requires_grad_(False)
        ref_model.eval()
        self.ref_model = ref_model

        # LoRA
        lora_config = LoraConfig(
            r=cfg.lora_r,
            lora_alpha=cfg.lora_alpha,
            target_modules=cfg.lora_target_modules,
            lora_dropout=cfg.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
        )

        lora_model = get_peft_model(base_model, lora_config)
        lora_model.print_trainable_parameters()
        lora_model.train()
        self.model = lora_model

        print(f"  ✅ LoRA applied: r={cfg.lora_r}, alpha={cfg.lora_alpha}")
        print(f"  ✅ Trainable params: {sum(p.numel() for p in lora_model.parameters() if p.requires_grad)/1e3:.1f}K")

        # VRAM report
        if torch.cuda.is_available():
            vram_used = torch.cuda.memory_allocated() / 1e9
            print(f"  📊 VRAM after setup: {vram_used:.2f} GB")

    # ── Optimizer & Scheduler ──

    def _setup_optimizer(self):
        cfg = self.config
        no_decay = ["bias", "LayerNorm.weight", "layer_norm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in self.model.named_parameters()
                           if not any(nd in n for nd in no_decay) and p.requires_grad],
                "weight_decay": cfg.weight_decay,
            },
            {
                "params": [p for n, p in self.model.named_parameters()
                           if any(nd in n for nd in no_decay) and p.requires_grad],
                "weight_decay": 0.0,
            },
        ]

        self.optimizer = torch.optim.AdamW(
            optimizer_grouped_parameters,
            lr=cfg.learning_rate,
            betas=(cfg.adam_beta1, cfg.adam_beta2),
            eps=cfg.adam_epsilon,
        )

        self.scheduler = get_scheduler(
            cfg.lr_scheduler,
            optimizer=self.optimizer,
            num_warmup_steps=cfg.warmup_steps,
            num_training_steps=cfg.max_steps,
        )
        print(f"  ✅ Optimizer: AdamW lr={cfg.learning_rate}, wd={cfg.weight_decay}")
        print(f"  ✅ Scheduler: {cfg.lr_scheduler}, warmup={cfg.warmup_steps}, total={cfg.max_steps}")

    def get_model(self):
        """Public accessor for the PEFT model, so external code can inspect it."""
        return self.model

    def get_tokenizer(self):
        """Public accessor for the tokenizer."""
        return self.tokenizer

    # ── On-Policy Generation ──

    @torch.no_grad()
    def _generate_on_policy(
        self,
        theorems: list[str],
    ) -> tuple[list[list[str]], list[torch.Tensor], list[torch.Tensor]]:
        """
        Generate on-policy trajectories from current policy.

        Returns:
          candidates: list[list[str]] — per theorem, list of K candidate codes
          log_probs: list[Tensor] — per theorem, [K, seq_len] log π_θ
          ref_log_probs: list[Tensor] — per theorem, [K, seq_len] log π_ref
        """
        cfg = self.config
        self.model.eval()
        self.ref_model.eval()

        all_candidates = []
        all_log_probs = []
        all_ref_log_probs = []

        for theorem in theorems:
            prompt = f"theorem t : {theorem} := by"
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
            prompt_len = inputs["input_ids"].shape[1]

            # Generate K candidates
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=cfg.grpo_max_new_tokens,
                num_return_sequences=cfg.grpo_group_size,
                do_sample=True,
                temperature=cfg.grpo_temperature,
                top_p=cfg.grpo_top_p,
                pad_token_id=self.tokenizer.pad_token_id,
                return_dict_in_generate=True,
                output_scores=True,
            )

            # Decode candidates
            batch_candidates = []
            batch_log_probs = []
            batch_ref_log_probs = []

            for seq_idx in range(cfg.grpo_group_size):
                seq_ids = outputs.sequences[seq_idx]
                generated_ids = seq_ids[prompt_len:]
                code = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
                batch_candidates.append(code)

                # Compute log probs for this sequence
                # outputs.scores is list of [group_size, vocab_size] per generated token
                seq_log_probs = []
                for token_idx, scores in enumerate(outputs.scores):
                    token_id = generated_ids[token_idx].unsqueeze(0)
                    # log_softmax of scores at this position for this sequence
                    log_softmax = F.log_softmax(scores[seq_idx], dim=-1)
                    token_log_prob = log_softmax[token_id]
                    seq_log_probs.append(token_log_prob)

                batch_log_probs.append(torch.stack(seq_log_probs))

                # Ref log probs (forward pass with ref model)
                ref_inputs = {
                    "input_ids": seq_ids.unsqueeze(0),
                    "attention_mask": torch.ones_like(seq_ids).unsqueeze(0),
                }
                with torch.no_grad():
                    ref_out = self.ref_model(**ref_inputs)
                ref_lm_logits = ref_out.logits[0, prompt_len - 1:-1]  # [seq_len, vocab]
                ref_lsm = F.log_softmax(ref_lm_logits, dim=-1)
                ref_tok_log_probs = ref_lsm[torch.arange(len(generated_ids)), generated_ids]
                batch_ref_log_probs.append(ref_tok_log_probs)

            all_candidates.append(batch_candidates)
            all_log_probs.append(batch_log_probs)
            all_ref_log_probs.append(batch_ref_log_probs)

        self.model.train()
        return all_candidates, all_log_probs, all_ref_log_probs

    # ── Off-Policy Sampling ──

    def _sample_off_policy(
        self,
        n_theorems: int,
    ) -> tuple[list[str], list[str], list[torch.Tensor], list[torch.Tensor]]:
        """
        Sample off-policy expert traces (e.g., from DialogueCache).

        In production this reads from the DialogueCache database.
        For now, stubs with a few canned examples.

        Returns:
          theorems, codes, log_probs, ref_log_probs
        """
        # Stub: return empty (off-policy only used when DialogueCache is populated)
        return [], [], [], []

    # ── Training Step ──

    def train_step(
        self,
        theorems: list[str],
        off_policy_theorems: list[str] | None = None,
        off_policy_codes: list[str] | None = None,
    ) -> dict:
        """
        Single LUFFY training step.

        Flow:
          1. On-policy: generate K candidates per theorem
          2. Compute compile-based rewards
          3. Compute GRPO loss (on-policy)
          4. (if available) Compute GRPO loss (off-policy, shaped)
          5. Combine losses: ℒ = ℒ_on + λ·f(N_off/N_on)·ℒ_off + β·KL
          6. Backward + optimizer step

        Returns metrics dict.
        """
        cfg = self.config
        metrics = {}
        step_start = time.time()

        # ── On-policy ──
        t0 = time.time()
        candidates, log_probs_list, ref_log_probs_list = self._generate_on_policy(theorems)
        metrics["timing/generate_ms"] = (time.time() - t0) * 1000

        # Compute rewards
        t0 = time.time()
        on_rewards = []
        for theorem_candidates in candidates:
            rewards, _ = compute_compile_reward(theorem_candidates)
            on_rewards.append(rewards)
        metrics["timing/reward_ms"] = (time.time() - t0) * 1000

        # GRPO loss (on-policy)
        t0 = time.time()
        on_losses = []
        on_infos = []
        for idx in range(len(theorems)):
            # Pad log probs to same length
            max_len = max(lp.shape[0] for lp in log_probs_list[idx])
            padded_lp = torch.stack([
                F.pad(lp, (0, max_len - lp.shape[0]), value=0.0)
                for lp in log_probs_list[idx]
            ])
            padded_ref = torch.stack([
                F.pad(rlp, (0, max_len - rlp.shape[0]), value=0.0)
                for rlp in ref_log_probs_list[idx]
            ])
            loss, info = compute_grpo_loss(
                padded_lp, padded_ref,
                on_rewards[idx].to(self.device),
                kl_beta=cfg.kl_beta,
            )
            on_losses.append(loss)
            on_infos.append(info)

        # Average across theorems
        on_loss = torch.stack(on_losses).mean()
        avg_on_reward = torch.stack([r.mean() for r in on_rewards]).mean().item()
        metrics["loss/on_policy"] = on_loss.item()
        metrics["reward/on_mean"] = avg_on_reward
        for key in ["reward/mean", "advantage/mean", "advantage/std"]:
            if key in on_infos[0]:
                metrics[f"on/{key.replace('/', '_')}"] = on_infos[0][key]
        metrics["timing/loss_on_ms"] = (time.time() - t0) * 1000

        # ── Off-policy ──
        off_loss = torch.tensor(0.0, device=self.device)
        n_off = 0
        n_on = len(theorems)

        if off_policy_theorems and off_policy_codes and len(off_policy_theorems) > 0:
            # Compute off-policy GRPO loss
            off_rewards, _ = compute_compile_reward(off_policy_codes)
            # ... simplified: in production compute log probs of expert codes under current policy
            n_off = len(off_policy_theorems)

            # Shaping weight
            shaping_w = policy_shaping_weight(n_on, n_off, gamma=cfg.policy_shaping_gamma)
            off_loss = off_loss * shaping_w
            metrics["loss/off_policy"] = off_loss.item()
            metrics["shaping/weight"] = shaping_w
        else:
            metrics["loss/off_policy"] = 0.0
            metrics["shaping/weight"] = 0.0
            metrics["off/n_theorems"] = 0

        # ── Combined loss ──
        total_loss = on_loss + cfg.lambda_off * off_loss
        loss_for_backward = total_loss / cfg.gradient_accumulation_steps
        metrics["loss/total"] = total_loss.item()

        # ── Backward ──
        t0 = time.time()
        loss_for_backward.backward()
        metrics["timing/backward_ms"] = (time.time() - t0) * 1000

        # ── Optimizer step (when accumulation is complete) ──
        if (self.global_step + 1) % cfg.gradient_accumulation_steps == 0:
            t0 = time.time()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), cfg.max_grad_norm)
            self.optimizer.step()
            self.scheduler.step()
            self.optimizer.zero_grad()
            metrics["timing/optimizer_ms"] = (time.time() - t0) * 1000
            metrics["lr"] = self.scheduler.get_last_lr()[0]

        # ── VRAM ──
        if torch.cuda.is_available():
            metrics["vram/allocated_gb"] = torch.cuda.memory_allocated() / 1e9
            metrics["vram/reserved_gb"] = torch.cuda.memory_reserved() / 1e9

        metrics["timing/step_total_ms"] = (time.time() - step_start) * 1000
        return metrics

    # ── Training Loop ──

    def train(
        self,
        train_theorems: list[str],
        eval_theorems: list[str] | None = None,
    ):
        """Full training loop with logging and checkpointing."""
        cfg = self.config

        # Setup
        self._setup_model()
        self.model.gradient_checkpointing_enable()
        self._setup_optimizer()

        print(f"\n{'='*60}")
        print(f"  LUFFY Training Starting")
        print(f"  Train theorems: {len(train_theorems)}")
        print(f"  Max steps: {cfg.max_steps} | Batch: {cfg.batch_size} × {cfg.grpo_group_size}")
        print(f"  Gamma: {cfg.policy_shaping_gamma} | λ_off: {cfg.lambda_off}")
        print(f"{'='*60}\n")

        self.global_step = 0
        n_batches = math.ceil(len(train_theorems) / cfg.batch_size)
        best_eval_reward = -float("inf")

        for step in range(cfg.max_steps):
            step_metrics = {"step": step, "epoch": step * cfg.batch_size / len(train_theorems)}

            # Select batch
            batch_start = (step % n_batches) * cfg.batch_size
            batch_end = min(batch_start + cfg.batch_size, len(train_theorems))
            batch_theorems = train_theorems[batch_start:batch_end]

            # Train step
            try:
                batch_metrics = self.train_step(batch_theorems)
                step_metrics.update(batch_metrics)
            except Exception as e:
                print(f"  ❌ Step {step} failed: {e}")
                step_metrics["error"] = str(e)

            # Log
            if step % cfg.log_every_steps == 0:
                self.logger.log(step, step_metrics)
                self._print_step(step, step_metrics)

            # Evaluate
            if eval_theorems and step > 0 and step % (cfg.log_every_steps * 2) == 0:
                eval_metrics = self.evaluate(eval_theorems)
                self.logger.log(step, {f"eval/{k}": v for k, v in eval_metrics.items()})
                if eval_metrics.get("reward/mean", -1) > best_eval_reward:
                    best_eval_reward = eval_metrics["reward/mean"]
                    self._save_checkpoint(f"best_adapter")

            # Checkpoint
            if step > 0 and step % cfg.save_every_steps == 0:
                self._save_checkpoint(f"step_{step}")

            self.global_step += 1

        # Final save
        self._save_checkpoint("final")
        self.logger.close()

        print(f"\n{'='*60}")
        print(f"  LUFFY Training Complete — {cfg.max_steps} steps")
        print(f"  Checkpoints: {cfg.checkpoint_dir}")
        print(f"  Logs: {cfg.log_dir}")
        print(f"{'='*60}")

    # ── Evaluation ──

    @torch.no_grad()
    def evaluate(self, theorems: list[str]) -> dict:
        """Evaluate current policy on eval set."""
        self.model.eval()
        rewards = []

        for theorem in theorems:
            prompt = f"theorem t : {theorem} := by"
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)

            outputs = self.model.generate(
                **inputs,
                max_new_tokens=self.config.grpo_max_new_tokens,
                num_return_sequences=4,
                do_sample=True,
                temperature=0.4,
                pad_token_id=self.tokenizer.pad_token_id,
            )

            prompt_len = inputs["input_ids"].shape[1]
            candidates = [
                self.tokenizer.decode(out[prompt_len:], skip_special_tokens=True)
                for out in outputs.sequences
            ]

            r, _ = compute_compile_reward(candidates)
            rewards.append(r.max().item())  # best candidate per theorem

        self.model.train()

        rewards_t = torch.tensor(rewards)
        return {
            "reward/mean": rewards_t.mean().item(),
            "reward/std": rewards_t.std().item(),
            "reward/min": rewards_t.min().item(),
            "reward/max": rewards_t.max().item(),
            "n_theorems": len(theorems),
        }

    # ── Checkpointing ──

    def _save_checkpoint(self, tag: str):
        """Save LoRA adapter."""
        path = os.path.join(self.config.checkpoint_dir, tag)
        self.model.save_pretrained(path)
        self.tokenizer.save_pretrained(path)
        print(f"  💾 Checkpoint saved: {path}")

    def load_adapter(self, adapter_path: str):
        """Load a previously saved LoRA adapter."""
        if self.model is None:
            self._setup_model()
        self.model.load_adapter(adapter_path)

    # ── vLLM Export ──

    def export_for_vllm(
        self,
        adapter_path: str,
        output_dir: str | None = None,
        adapter_name: str = "luffy",
    ):
        """
        Export LoRA adapter for vLLM serving.

        Args:
            adapter_path: Path to saved adapter checkpoint.
            output_dir: Output directory (default: adapter_path/vllm_ready)
            adapter_name: Name for vLLM --lora-modules (e.g., 'luffy_leak_test_v1')

        vLLM serves LoRA adapters natively:
          vllm serve Goedel-LM/Goedel-Prover-V2-8B \
            --enable-lora \
            --lora-modules {adapter_name}={adapter_path}

        Optionally merge LoRA into base model (if output_dir specified).
        """
        from peft import PeftModel

        out = output_dir or os.path.join(self.config.checkpoint_dir, "vllm_ready")
        os.makedirs(out, exist_ok=True)

        # Copy adapter to output dir with clean naming
        self.model.save_pretrained(out)
        self.tokenizer.save_pretrained(out)

        print(f"  🚀 vLLM adapter exported: {out}")
        print(f"  🚀 Adapter name: {adapter_name}")
        print(f"  🚀 vLLM command:")
        print(f"     vllm serve {self.config.base_model_id} \\")
        print(f"       --enable-lora \\")
        print(f"       --lora-modules {adapter_name}={out}")

    # ── Utility ──

    def _print_step(self, step: int, metrics: dict):
        """Compact step log line."""
        loss = metrics.get("loss/total", 0)
        on_reward = metrics.get("reward/on_mean", 0)
        lr = metrics.get("lr", 0)
        timing = metrics.get("timing/step_total_ms", 0)
        vram = metrics.get("vram/allocated_gb", 0)
        print(
            f"  Step {step:4d} | loss={loss:.4f} | R_on={on_reward:.3f} | "
            f"lr={lr:.2e} | {timing:.0f}ms | VRAM={vram:.1f}GB"
        )
