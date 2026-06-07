#!/usr/bin/env bash
# Serve Goedel-Prover-V2 via vLLM with OpenAI-compatible API.
# Usage: bash scripts/serve-goedel.sh [model]
#   model: 8b (default), 32b

set -euo pipefail

MODEL="${1:-8b}"

case "$MODEL" in
  8b|8B)
    HF_MODEL="Goedel-LM/Goedel-Prover-V2-8B"
    PORT=8001
    ;;
  32b|32B)
    HF_MODEL="Goedel-LM/Goedel-Prover-V2-32B"
    PORT=8001
    ;;
  *)
    echo "Usage: $0 [8b|32b]"
    exit 1
    ;;
esac

echo "🔬 Starting vLLM server: $HF_MODEL on port $PORT"
echo "   API endpoint: http://localhost:$PORT/v1"
echo "   Use: omega prove \"...\" --model goedel/goedel-v2-${MODEL}"
echo ""

python -m vllm.entrypoints.openai.api_server \
    --model "$HF_MODEL" \
    --port "$PORT" \
    --max-model-len 16384 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.90
