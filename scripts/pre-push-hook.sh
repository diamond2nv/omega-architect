#!/usr/bin/env bash
# Git pre-push hook for omega-architect
# Runs pytest (excluding slow/regression tests) before push.
# Install: bash scripts/install-hook.sh

set -euo pipefail

echo "🔬 Omega Pre-Push: Running quick test suite..."
echo ""

cd "$(git rev-parse --show-toplevel)"

if python3 -m pytest tests/ -q --ignore=tests/test_regression_fixes.py -k "not budget" 2>&1; then
    echo ""
    echo "✅ All tests passed. Pushing..."
    exit 0
else
    echo ""
    echo "❌ Pre-push check FAILED: test regression detected."
    echo "   Fix failing tests before pushing."
    exit 1
fi
