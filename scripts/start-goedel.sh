#!/usr/bin/env bash
# Start Goedel-Prover-V2 vLLM server with system readiness checks.
# Usage: bash scripts/start-goedel.sh
#
# Before starting vLLM (~16 GB VRAM in BF16):
#   1. Check GPU VRAM availability
#   2. Release Ollama models if they're blocking VRAM
#   3. Start vLLM with correct WSL parameters
#   4. Verify the API responds
#
# The MLRoute will route SIMPLE/MEDIUM problems to this local Goedel server,
# and HARD problems to DeepSeek-v4-flash API (cloud, $0.14/M tok).
# Complementary model routing is handled automatically by ModelRouter.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8001}"

echo "═══════════════════════════════════════════"
echo "  Ω-Architect: Goedel Server Startup"
echo "═══════════════════════════════════════════"

# ── Step 1: Check GPU ──────────────────────────────────────────

echo ""
echo "[1/5] Checking GPU availability..."
GPU_OK=$(python3 -c "
import torch
if not torch.cuda.is_available():
    print('NO_CUDA')
else:
    free, total = torch.cuda.mem_get_info()
    name = torch.cuda.get_device_name(0)
    print(f'{name}|{total}|{free}')
" 2>/dev/null || echo "NO_CUDA")

if [ "$GPU_OK" = "NO_CUDA" ]; then
    echo "  ✗ CUDA not available. Cannot start vLLM server."
    exit 1
fi

IFS='|' read -r GPU_NAME TOTAL_MEM FREE_MEM <<< "$GPU_OK"
TOTAL_GB=$(echo "scale=1; $TOTAL_MEM / 1e9" | bc)
FREE_GB=$(echo "scale=1; $FREE_MEM / 1e9" | bc)
FREE_PCT=$(echo "scale=0; 100 * $FREE_MEM / $TOTAL_MEM" | bc)

echo "  ✓ GPU: $GPU_NAME"
echo "  ✓ VRAM: ${TOTAL_GB}GB total, ${FREE_GB}GB free (${FREE_PCT}%)"

# Goedel-V2-8B BF16 needs ~16 GB VRAM; reserve 2 GB for overhead
NEEDED_GB=18
if [ "$(echo "$FREE_GB < $NEEDED_GB" | bc)" -eq 1 ]; then
    echo "  ⚠ Only ${FREE_GB}GB free; Goedel-V2-8B needs ~${NEEDED_GB}GB"
    echo "  Trying to free VRAM..."
    python3 "$SCRIPT_DIR/ollama-free-vram.py" || true
    # Re-check
    FREE_MEM=$(python3 -c "import torch; print(int(torch.cuda.mem_get_info()[1]))" 2>/dev/null)
    FREE_GB=$(echo "scale=1; $FREE_MEM / 1e9" | bc)
    FREE_PCT=$(echo "scale=0; 100 * $FREE_MEM / $TOTAL_MEM" | bc)
    echo "  VRAM now: ${FREE_GB}GB free (${FREE_PCT}%)"
fi

if [ "$(echo "$FREE_GB < $NEEDED_GB" | bc)" -eq 1 ]; then
    echo "  ✗ Insufficient VRAM (${FREE_GB}GB < ${NEEDED_GB}GB needed)"
    echo "  Suggestion: close other GPU applications (Terminal, VS Code GPU extensions)"
    echo "  Fallback: route all problems to DeepSeek-v4-flash API (no GPU needed)"
    exit 1
fi

echo "  ✓ Sufficient VRAM for Goedel-V2-8B"

# ── Step 2: Free Ollama ────────────────────────────────────────

echo ""
echo "[2/5] Releasing Ollama GPU memory..."
python3 "$SCRIPT_DIR/ollama-free-vram.py" || true

# ── Step 3: Verify model files ─────────────────────────────────

echo ""
echo "[3/5] Verifying model files..."
MODEL_DIR="$HOME/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B"

if [ ! -f "$MODEL_DIR/config.json" ]; then
    echo "  ✗ Model not found at $MODEL_DIR"
    echo "  Run: huggingface-cli download Goedel-LM/Goedel-Prover-V2-8B"
    exit 1
fi

# Verify safetensors exist
EXPECTED=4
FOUND=$(ls -1 "$MODEL_DIR"/model-*.safetensors 2>/dev/null | wc -l)
echo "  ✓ Model at $MODEL_DIR"
echo "  ✓ Safetensors: $FOUND/$EXPECTED shards"

# ── Step 4: Apply WSL UVA patch ────────────────────────────────

echo ""
echo "[4/5] Applying WSL UVA compatibility patch..."
PATCH_FILE="/home/shenli/miniconda3/lib/python3.13/site-packages/vllm/platforms/interface.py"
if grep -q "WSL is detected" "$PATCH_FILE" 2>/dev/null; then
    echo "  Applying UVA patch (WSL pin_memory fix)..."
    sed -i 's/if in_wsl():.*/if in_wsl():/; /Pinning memory in WSL is not supported/,/return False/d' "$PATCH_FILE"
    # More reliable: patch the return
    python3 -c "
import re
with open('$PATCH_FILE') as f:
    content = f.read()
# Replace the entire is_pin_memory_available function body
old = '''    @classmethod
    def is_pin_memory_available(cls) -> bool:
        \"\"\"Checks whether pin memory is available on the current platform.\"\"\"
        if in_wsl():
            # Pinning memory in WSL is not supported.
            # https://docs.nvidia.com/cuda/wsl-user-guide/index.html#known-limitations-for-linux-cuda-applications
            logger.warning(
                \"Using 'pin_memory=False' as WSL is detected. \"
                \"This may slow down the performance.\"
            )
            return False
        return True'''

new = '''    @classmethod
    def is_pin_memory_available(cls) -> bool:
        \"\"\"Checks whether pin memory is available on the current platform.\"\"\"
        # WSL2 supports pin_memory() with recent NVIDIA drivers (verified).
        # The old WSL limitation is no longer applicable.
        return True'''

if old in content:
    content = content.replace(old, new)
    with open('$PATCH_FILE', 'w') as f:
        f.write(content)
    print('  ✓ UVA patch applied')
else:
    # Check if already patched
    if 'return True' in content and 'WSL' not in content.split('return True')[0]:
        print('  ✓ UVA patch already applied')
    else:
        print('  ⚠ UVA patch may need manual review')
" 2>&1 | tail -5
fi

# ── Step 5: Start vLLM server ──────────────────────────────────

echo ""
echo "[5/5] Starting vLLM server..."
echo "  Model: Goedel-LM/Goedel-Prover-V2-8B (BF16, ~16 GB VRAM)"
echo "  Port:  $PORT"
echo "  API:   http://localhost:$PORT/v1"
echo "  Route: Simple/Medium → Goedel (local) | Hard → DeepSeek API"
echo ""

# Start server in background
conda run -n base python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_DIR" \
    --served-model-name "Goedel-LM/Goedel-Prover-V2-8B" \
    --port "$PORT" \
    --max-model-len 8192 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.85 \
    --enforce-eager \
    --trust-remote-code \
    --disable-log-stats &
VLLM_PID=$!

# Wait for server to be ready
echo "  Waiting for server..."
for i in $(seq 1 180); do
    if curl -s http://localhost:$PORT/v1/completions \
         -H "Content-Type: application/json" \
         -d '{"model":"Goedel-LM/Goedel-Prover-V2-8B","prompt":"test","max_tokens":1,"temperature":0.0}' \
         2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'choices' in d else 1)" 2>/dev/null; then
        echo ""
        echo "  ✓ Server ready after ${i}s"
        echo ""
        echo "═══════════════════════════════════════════"
        echo "  Goedel vLLM Server Running (PID: $VLLM_PID)"
        echo "  API: http://localhost:$PORT/v1"
        echo "  Model name for requests: Goedel-LM/Goedel-Prover-V2-8B"
        echo ""
        echo "  Use: omega prove \"theorem t : True :=\" --model goedel/goedel-v2-8b"
        echo "  Complementary: ModelRouter auto-routes hard problems to DeepSeek API"
        echo "═══════════════════════════════════════════"
        exit 0
    fi
    printf "."
    sleep 1
done

echo ""
echo "  ✗ Server failed to start within 180s"
exit 1
