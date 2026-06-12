#!/usr/bin/env bash
# Git pre-push hook for omega-architect
# Runs pytest (excluding slow/regression + optional-dependency tests) before push.
# Install: bash scripts/install-hook.sh

set -euo pipefail

echo "🔬 Omega Pre-Push: Running quick test suite..."
echo ""

cd "$(git rev-parse --show-toplevel)"

# Run core tests only (skip tests needing optional deps like peft/lightgbm/dspy/json_repair)
if python3 -m pytest tests/ -q \
    --ignore=tests/test_regression_fixes.py \
    --ignore=tests/test_model_router_v2.py \
    --ignore=tests/test_playbook.py \
    --ignore=tests/test_generate_fn.py \
    --ignore=tests/test_luffy_trainer.py \
    -k "not budget and not dspy and not lightgbm and not onnx" \
    2>&1; then
    echo ""
    echo "✅ Core tests passed. Pushing..."
    exit 0
else
    echo ""
    echo "❌ Pre-push check FAILED: core test regression detected."
    echo "   Fix failing tests before pushing."
    exit 1
fi
