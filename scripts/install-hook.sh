# Install Omega pre-push hook
# Run from repo root: bash scripts/install-hook.sh
set -euo pipefail

HOOK_SOURCE="scripts/pre-push-hook.sh"
HOOK_TARGET=".git/hooks/pre-push"

if [ ! -f "$HOOK_SOURCE" ]; then
    echo "❌ Hook source not found: $HOOK_SOURCE"
    exit 1
fi

ln -sf "../../$HOOK_SOURCE" "$HOOK_TARGET"
chmod +x "$HOOK_TARGET"
echo "✅ Pre-push hook installed: $HOOK_TARGET → $HOOK_SOURCE"
echo "   Runs 'omega benchmark theorems --tiers 1 --auto' on git push."
