#!/usr/bin/env bash
# Git pre-push hook for omega-architect
# Runs `omega benchmark theorems --tiers 1 --auto` before push.
# Install: ln -sf ../../scripts/pre-push-hook.sh .git/hooks/pre-push

set -euo pipefail

echo "🔬 Omega Pre-Push: Running Tier 1 benchmark auto-check..."
echo ""

# Try running the benchmark
if python3 -m omega.cli benchmark theorems --tiers 1 --auto --samples 2 --rounds 1 --timeout 60 2>&1; then
    echo ""
    echo "✅ Pre-push check passed. Pushing..."
    exit 0
else
    echo ""
    echo "❌ Pre-push check FAILED: Tier 1 benchmark regression detected."
    echo "   Review changes and fix before pushing."
    exit 1
fi
