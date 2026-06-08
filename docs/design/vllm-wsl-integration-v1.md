# vLLM WSL Integration for Goedel-Prover-V2

> Hard-won lessons serving LLMs on WSL (CUDA 13.0) with vLLM

## 1. Environment

| Component | Value |
|-----------|-------|
| Host OS | Windows 11 WSL2 (Ubuntu) |
| GPU | NVIDIA RTX 4500 Ada (24 GiB) |
| CUDA Runtime (PyTorch) | 13.0 |
| PyTorch | 2.11.0+cu130 |
| vLLM | 0.22.1 (latest pip, V1 engine) |
| Transformers | 4.56.2 |
| bitsandbytes | 0.49.2 |
| Model | Goedel-LM/Goedel-Prover-V2-8B (~8B params) |

## 2. Root Cause Analysis

### 2.1 The "Engine Core Failed" Mystery

Initial symptom: vLLM crashes with a non-informative error:

```
RuntimeError: Engine core initialization failed.
See root cause above. Failed core proc(s): {}
```

Key finding: **the real error is in the EngineCore child process's stderr**, hidden by the `| tail -20` pipe in the launch command. Full log extraction revealed:

```
ValueError: Free memory on device cuda:0 (10.41/22.49 GiB)
on startup is less than desired GPU memory utilization
(0.85, 19.12 GiB). Decrease GPU memory utilization.
```

### 2.2 WSL GPU Memory Condition

On WSL2, GPU memory is **shared with Windows**. Windows reserves ~13 GiB for:
- WSLg (GUI compositor)
- Display driver overhead
- Other Windows processes

This leaves only **~11 GiB free** out of 24 GiB total at vLLM startup:

```python
# After boot, no other GPU processes:
free, total = torch.cuda.mem_get_info(0)
# → free ≈ 11.17 GiB / 24.15 GiB  (46%)
```

### 2.3 The Real Resource Profile

With `--gpu-memory-utilization 0.40` (requests ~9.6 GiB):

```
Model weights (BF16): 15.27 GiB    ← loaded by PyTorch directly
KV cache:              4.27 GiB    ← limited by gpu-memory-utilization
Total:                 ~19.54 GiB  ← fits within 24 GiB total
```

Despite only 11 GiB "free" at measurement time, the model loads successfully at 0.40 utilization. This suggests that PyTorch uses CUDA memory allocator tricks (memory overcommit / fragmentation reduction) and the "free" metric is conservative.

## 3. Critical vLLM Flags for WSL

### 3.1 `--gpu-memory-utilization`

- **Default**: 0.90 → **Always fails** on WSL
- **Recommended**: `0.40` (stable with 8B BF16 on 24 GiB GPU with WSL overhead)
- **Tuning**: Lower if OOM during generation; increase if KV cache is bottleneck
- For GGUF/4-bit models: can use 0.60-0.80

### 3.2 `--served-model-name`

Without this flag, vLLM registers the model with its filesystem path:

```json
// Without --served-model-name:
{"id": "/home/.../models--Goedel-LM--Goedel-Prover-V2-8B", ...}

// With --served-model-name Goedel-LM/Goedel-Prover-V2-8B:
{"id": "Goedel-LM/Goedel-Prover-V2-8B", ...}
```

The client (`omega/llm.py`) sends `model="Goedel-LM/Goedel-Prover-V2-8B"` in API calls. Without `--served-model-name`, vLLM returns "model does not exist" because the names don't match.

### 3.3 Other Required Flags

| Flag | Reason |
|------|--------|
| `--enforce-eager` | Disables CUDAGraphs (not fully supported on WSL + CUDA 13.0) |
| `--trust-remote-code` | Required for custom model architectures (Qwen3 for Goedel-V2) |
| `--max-model-len 4096` | Shorter context = less KV cache memory pressure |
| `--dtype bfloat16` | Native BF16 support on Ada Lovelace; saves 50% VRAM vs FP32 |

### 3.4 WSL-Specific Warnings

```
WARNING: We must use the `spawn` multiprocessing start method.
Overriding VLLM_WORKER_MULTIPROC_METHOD to 'spawn'.
Reasons: WSL is detected and NVML is not compatible with fork
```

This is **normal** on WSL. vLLM auto-detects WSL and switches to `spawn`. No action needed.

## 4. Verified Workflow: Goedel-Prover-V2 Local Serving

### 4.1 Start Server

```bash
# Terminal 1
bash scripts/serve-goedel.sh 8b 8001
# Output after ~50s (model loading):
#   Model loading took 15.27 GiB and 48.30 seconds
#   Starting vLLM server on http://0.0.0.0:8001
```

### 4.2 Verify Health

```bash
curl http://localhost:8001/v1/models | jq .data[].id
# "Goedel-LM/Goedel-Prover-V2-8B"
```

### 4.3 Generate Proof

```bash
# Chat completions work (model generates natural language + Lean code)
curl -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Goedel-LM/Goedel-Prover-V2-8B",
    "messages": [{"role": "user", "content": "theorem hello : 1 + 1 = 2 := by"}],
    "temperature": 0.3,
    "max_tokens": 256
  }'

# Text completions also work (more direct for proof generation)
curl -s http://localhost:8001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Goedel-LM/Goedel-Prover-V2-8B",
    "prompt": "theorem hello : 1 + 1 = 2 := by",
    "temperature": 0.3,
    "max_tokens": 256
  }'
```

### 4.4 Use with Omega

```python
from omega.llm import resolve_generate_fn

# Auto-routes to http://localhost:8001/v1
generate = resolve_generate_fn("goedel/goedel-v2-8b")
result = generate("theorem t : True := by\n  trivial")
print(result)
# → "norm_num\n  <;> simp_all\n  <;> norm_num\n..."
```

## 5. Troubleshooting Checklist

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "Engine core initialization failed" | GPU OOM during KV cache alloc | Lower `--gpu-memory-utilization` (try 0.35-0.40) |
| "model does not exist" in chat | Missing `--served-model-name` | Add `--served-model-name Goedel-LM/...` |
| CUDA out of memory mid-request | KV cache too large | Lower `--max-model-len` or increase utilization |
| Very slow generation (>10s/token) | `enforce_eager` trades perf for compat | Acceptable tradeoff on WSL; try `--cuda-graph` if NVML works |
| Server hangs on startup | WSL spawn + NCCL timeout | `kill -9` all python children + retry |
| Chat completions return empty | Model has no chat template | Use `/v1/completions` instead |
| Overridden sampling params | Model's `generation_config.json` | Add `--generation-config vllm` to force vLLM defaults |

## 6. Performance Baseline (RTX 4500 Ada, WSL, BF16)

| Metric | Value |
|--------|-------|
| Model load time | 48 s |
| Max concurrency | 7.59× (4096 token prompt) |
| Effective KV cache | 31,072 tokens |
| Inference (Eager mode) | ~3-5 tok/s (estimated) |
| GPU memory - weights | 15.27 GiB |
| GPU memory - KV cache | 4.27 GiB |
| GPU memory - total used | ~19.5 GiB |

## 7. Alternatives Considered

### 7.1 vLLM V0 Engine

vLLM 0.22.1 has **no V0 engine** — V1 is the only option. The environment variable `VLLM_USE_V1=0` is unrecognized and ignored.

### 7.2 NVIDIA NGC Container (`nvcr.io/nvidia/vllm:25.09`)

CUDA 13.0 native, optimized for NVIDIA GPUs. Could be run via Podman with GPU passthrough. Pro: no build-from-source needed. Con: container overhead on WSL.

### 7.3 Transformers + bitsandbytes (4-bit)

Working but **2-3× slower** than vLLM because:
- No KV cache optimization
- No continuous batching
- Software attention (FlashAttention2 may fail on WSL)

### 7.4 Build vLLM from Source (CUDA 13.0)

Not necessary — the pip wheel works at CUDA 13.0, just needs correct `--gpu-memory-utilization`.

## 8. Invocation Template

```bash
# Production-grade launch template
VLLM_USE_V1=1 python -m vllm.entrypoints.openai.api_server \
    --model /path/to/model \
    --port 8001 \
    --max-model-len 4096 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.40 \
    --enforce-eager \
    --trust-remote-code \
    --served-model-name "Goedel-LM/Goedel-Prover-V2-8B"
```

## 9. Key Lessons

1. **Check WSL memory before tuning vLLM** — `torch.cuda.mem_get_info()` reveals true free memory (~46% on 24 GiB → ~11 GiB)
2. **`--served-model-name` is mandatory** when serving from local paths, otherwise the API model name = ugly filesystem path
3. **Pipe truncation hides errors** — using `| tail -20` on vLLM output hides the EngineCore child process error; always capture full output on first run
4. **vLLM 0.22.1 V1 works on CUDA 13.0** — the pre-built wheel has cu13 kernels from `humming-kernels[cu13]`
5. **The `gpu-memory-utilization` error isn't about the model weights** — it's about the KV cache; model weights load via PyTorch's own allocator and ignore this flag
