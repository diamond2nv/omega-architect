#!/usr/bin/env bash
# Serve Goedel-Prover-V2 via vLLM with OpenAI-compatible API.
# Usage: bash scripts/serve-goedel.sh [8b|32b] [port]
#
# Uses vLLM V1 engine (WSL UVA patched). Model loaded from local HF cache.
# The UVA patch at vllm/platforms/interface.py:is_pin_memory_available()
# is required for WSL — see scripts/serve-goedel.sh for patch details.

set -euo pipefail

MODEL="${1:-8b}"
PORT="${2:-8001}"

# Local cache path (direct snaphot dir to avoid LLM API resolver timing out on slow network)
CACHE_ROOT="$HOME/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B"

case "$MODEL" in
  8b|8B)
    MODEL_PATH="$CACHE_ROOT"
    ;;
  32b|32B)
    echo "ERROR: 32B model not yet downloaded. Use: huggingface-cli download Goedel-LM/Goedel-Prover-V2-32B"
    exit 1
    ;;
  *)
    echo "Usage: $0 [8b|32b] [port]"
    exit 1
    ;;
esac

if [ ! -f "$MODEL_PATH/config.json" ]; then
    echo "ERROR: Model not found at $MODEL_PATH"
    echo "Run: huggingface-cli download Goedel-LM/Goedel-Prover-V2-8B"
    exit 1
fi

echo "=== Goedel vLLM Server ==="
echo "  Model: $MODEL_PATH"
echo "  Port:  $PORT"
echo "  API:   http://localhost:$PORT/v1"
echo "  Use:   omega prove \"theorem t : True :=\" --model goedel/goedel-v2-8b"
echo ""

# ── Free Ollama VRAM before loading vLLM ──
if command -v python3 &>/dev/null; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PYTHON_SCRIPT="$SCRIPT_DIR/ollama-free-vram.py"
    if [ -f "$PYTHON_SCRIPT" ]; then
        echo "[pre-flight] Releasing Ollama GPU VRAM (if any)..."
        python3 "$PYTHON_SCRIPT" || true
        echo "[pre-flight] Done."
        echo ""
    fi
fi

exec python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_PATH" \
    --port "$PORT" \
    --max-model-len 4096 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.40 \
    --enforce-eager \
    --trust-remote-code \
    --served-model-name "Goedel-LM/Goedel-Prover-V2-8B"
