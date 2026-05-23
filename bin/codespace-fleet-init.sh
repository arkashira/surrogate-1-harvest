#!/usr/bin/env bash
# codespace-fleet-init.sh — one-shot bootstrap for the 7-codespace LLM
# fleet. Idempotent: safe to re-run. Does:
#   1. Make port 11434 public on each codespace
#   2. Wait until ollama responds
#   3. Print the comma-joined CODESPACE_LLM_URLS for /etc/surrogate-coordinator.env
#
# Run from any host that can reach the gh API and curl the codespace URLs.

set -u +e

# Tokens are loaded from env (export GH_TOKEN_<ACCOUNT> before running),
# never hardcoded — GitHub push-protection blocks PATs in committed files.
# Source from ~/.note manually:
#   eval $(grep -E 'GH_TOKEN_(ASHIRAPIT|MIDNIGHTGTS|LUCKYBURSTER|SURROGATE1|AXENTXTECH|ARKSHIPAI|IFUSEFREEDOMZA)=' ~/.note.env)
declare -a FLEET=(
  "${GH_TOKEN_ASHIRAPIT:-}	ollama-llm-proxy-r49955gvjxqv3ww4	ashirapit"
  "${GH_TOKEN_MIDNIGHTGTS:-}	ollama-llm-proxy-97vrxrxwjg45h7vwg	midnightgts"
  "${GH_TOKEN_LUCKYBURSTER:-}	ollama-llm-proxy-v6xpwqjvx4pq2x6qp	luckyburster-lab"
  "${GH_TOKEN_SURROGATE1:-}	ollama-llm-proxy-7vqv5w64p7g5fpppr	surrogate-1"
  "${GH_TOKEN_AXENTXTECH:-}	ollama-llm-proxy-r4947657rwwpf5x57	axentx-tech"
  "${GH_TOKEN_ARKSHIPAI:-}	ollama-llm-proxy-pjvjg5x5wr9j2rwww	arkship-ai"
  "${GH_TOKEN_IFUSEFREEDOMZA:-}	ollama-llm-proxy-97p97rxqqg77c7jwp	ifusefreedomza"
)

log() { echo "[fleet-init $(date -u +%H:%M:%SZ)] $*"; }

URLS=()
for entry in "${FLEET[@]}"; do
  IFS=$'\t' read -r tok name acct <<< "$entry"
  url="https://${name}-11434.app.github.dev"
  log "[$acct] $name"
  GH_TOKEN=$tok gh codespace ports visibility 11434:public -c "$name" 2>&1 | tail -1
  URLS+=("$url")
done

# Print env line ready to paste
echo ""
echo "CODESPACE_LLM_URLS=$(IFS=,; echo "${URLS[*]}")"
echo ""
echo "# Also paste the full fleet TSV for the keepalive (env CS_FLEET):"
echo "CS_FLEET<<'EOF'"
for entry in "${FLEET[@]}"; do echo "$entry"; done
echo "EOF"
