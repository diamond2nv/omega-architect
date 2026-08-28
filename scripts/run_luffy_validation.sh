#!/usr/bin/env bash
# Run LUFFY validation training with QLoRA.
# Sets CUDA library paths for bitsandbytes compatibility.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Use datatrove conda env Python directly
DATATROVE_PYTHON="${DATATROVE_PYTHON:-$(command -v python)}"
DATATROVE_PREFIX="${DATATROVE_PREFIX:-${DATATROVE_PYTHON%/bin/python}}"

# bitsandbytes needs libnvJitLink.so.13 from nvidia/cu13 in the datatrove env
export LD_LIBRARY_PATH="$DATATROVE_PREFIX/lib/python3.10/site-packages/nvidia/cu13/lib:$DATATROVE_PREFIX/lib:$LD_LIBRARY_PATH"
export PATH="$DATATROVE_PREFIX/bin:$PATH"

echo "=== LUFFY QLoRA Validation Training ==="
echo "  Python: $DATATROVE_PYTHON"
echo "  CWD:    $PROJECT_DIR"
echo "  GPU:    $($DATATROVE_PYTHON -c 'import torch; print(torch.cuda.get_device_name(0))')"
echo "  VRAM:   $($DATATROVE_PYTHON -c 'import torch; print(f"{torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")')"
echo ""

cd "$PROJECT_DIR"

$DATATROVE_PYTHON -u scripts/run_luffy_validation.py
