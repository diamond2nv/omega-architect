#!/usr/bin/env bash
# Sync omega-qed-wgm-value-analysis to NAS DokuWiki
set -e

PAGE="omega-qed-wgm-value-analysis"
SRC="/home/shenli/Gitlab/Agentic4Sci/omega-architect/docs/research/qed/dokuwiki-omega-qed-wgm-value.txt"
WIKI_URL="http://192.168.0.25:33080/lib/exe/xmlrpc.php"
WIKI_USER="shenli"
WIKI_PASS="${DOKUWIKI_PASS:-}"

if [ -z "$WIKI_PASS" ]; then
  echo "Error: DOKUWIKI_PASS not set"
  echo "Usage: DOKUWIKI_PASS=xxx $0"
  exit 1
fi

CONTENT=$(cat "$SRC")

# DokuWiki XML-RPC: wiki.putPage
python3 -c "
import xmlrpc.client, sys
server = xmlrpc.client.ServerProxy('$WIKI_URL')
try:
    result = server.wiki.putPage('$PAGE', '$CONTENT', {'user': '$WIKI_USER', 'pass': '$WIKI_PASS'})
    print(f'✅ Page $PAGE synced successfully')
except Exception as e:
    print(f'❌ Failed: {e}')
    sys.exit(1)
"

echo "Done: $PAGE"
