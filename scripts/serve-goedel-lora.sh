#!/usr/bin/env bash
# Serve Goedel-Prover-V2 via vLLM with LoRA adapter support.
# Usage: bash scripts/serve-goedel-lora.sh [adapter_path] [port]
#
# Prerequisites:
#   1. Train adapter via LUFFY trainer: python -c "
#      from omega.learn.rl.luffy_mixed_trainer import LuffyTrainerConfig, LuffyMixedPolicyTrainer
#      trainer = LuffyMixedPolicyTrainer(LuffyTrainerConfig())
#      trainer.export_for_vllm('./adapters/luffy_v1')
#    "
#   2. Or use any PEFT adapter directory with adapter_config.json + adapter_model.safetensors
#
# Multi-adapter:
#   bash scripts/serve-goedel-lora.sh \
#     "algo=./adapters/algebra,num=./adapters/number,ind=./adapters/induction" \
#     8001

set -euo pipefail

ADAPTER_SPEC="${1:-}"
PORT="${2:-8001}"

# Model from local HF cache
MODEL_PATH="$HOME/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B"

if [ ! -f "$MODEL_PATH/config.json" ]; then
    echo "ERROR: Model not found at $MODEL_PATH"
    exit 1
fi

LORA_ARGS=""
if [ -n "$ADAPTER_SPEC" ]; then
    # Parse adapter spec: "name1=path1,name2=path2" or just "path" (auto-named)
    if [[ "$ADAPTER_SPEC" == *"="* ]]; then
        LORA_ARGS="--enable-lora --lora-modules $ADAPTER_SPEC"
    else
        ADAPTER_NAME="${3:-luffy}"
        LORA_ARGS="--enable-lora --lora-modules ${ADAPTER_NAME}=${ADAPTER_SPEC}"
    fi
    echo "=== LoRA Adapters ==="
    echo "  $LORA_ARGS"
fi

echo "=== Goedel vLLM Server (LoRA-enabled) ==="
echo "  Model: $MODEL_PATH"
echo "  Port:  $PORT"
echo "  API:   http://localhost:$PORT/v1"
echo ""

# Free Ollama VRAM
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/ollama-free-vram.py" ]; then
    echo "[pre-flight] Releasing Ollama GPU VRAM..."
    python3 "$SCRIPT_DIR/ollama-free-vram.py" || true
fi

# vLLM 0.22.1: LoRA supported via --enable-lora + --lora-modules
exec python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_PATH" \
    --port "$PORT" \
    --max-model-len 4096 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.40 \
    --enforce-eager \
    --trust-remote-code \
    --served-model-name "Goedel-LM/Goedel-Prover-V2-8B" \
    $LORA_ARGS
