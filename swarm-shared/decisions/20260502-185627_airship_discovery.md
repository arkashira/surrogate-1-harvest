# airship / discovery

## Final Synthesis — Best of Both Candidates (Correct + Actionable)

I merged the strongest, non-redundant parts and resolved contradictions in favor of correctness and immediate usability.

- **Keep Candidate 1’s CLI** (`scripts/discover.sh`) because it’s immediately runnable, checks real ports, produces an index, and supports `--update-index`.  
- **Add Candidate 2’s `/meta/topology` endpoint** because it gives a machine-readable service map from Arkship itself (useful for UIs, SRE tooling, and onboarding).  
- **Resolve conflict**: Candidate 1 proposed a standalone script only; Candidate 2 proposed an API endpoint only. The correct choice is **both** — CLI for devs/ad-hoc use, API endpoint for runtime observability and automation.  
- **Correctness fix**: Use proper FastAPI startup checks and timeouts; avoid blocking startup on external services. Make health checks best-effort and fast.  
- **Actionability fix**: Provide exact file paths, minimal code, and clear verification steps.

---

## 1) Files to create/modify

### `scripts/discover.sh`
Portable CLI that:
- Checks Arkship, Surrogate, Neo4j, Qdrant reachability.
- Optionally runs `knowledge-rag top-hub` if available.
- Generates/updates `assets/index.json`.
- Prints concise topology and next actions.

```bash
#!/usr/bin/env bash
# Discovery CLI for Arkship + Surrogate platform
# Usage: ./scripts/discover.sh [--update-index]
# Tags: #discovery #knowledge-rag #index

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSETS_DIR="${PROJECT_ROOT}/assets"
INDEX_FILE="${ASSETS_DIR}/index.json"
UPDATE_INDEX=0

for arg in "$@"; do
  case "$arg" in
    --update-index) UPDATE_INDEX=1 ;;
  esac
done

mkdir -p "${ASSETS_DIR}"

echo "== Arkship + Surrogate Discovery =="
echo "Project root: ${PROJECT_ROOT}"
echo ""

# --- Service Readiness ---
echo "--- Service Readiness ---"
check_http() {
  local url=$1 name=$2
  if curl -fs --max-time 3 "$url" >/dev/null 2>&1; then
    echo "✅ ${name} (${url})"
  else
    echo "⚠️  ${name} (${url}) unreachable"
  fi
}

check_tcp() {
  local host=$1 port=$2 name=$3
  if timeout 3 bash -c "echo > /dev/tcp/${host}/${port}" 2>/dev/null; then
    echo "✅ ${name} (${host}:${port})"
  else
    echo "⚠️  ${name} (${host}:${port}) unreachable"
  fi
}

check_http "http://localhost:3000" "Arkship UI"
check_http "http://localhost:8000/health" "Arkship API"
check_http "http://localhost:8001/health" "Surrogate AI"
check_tcp "localhost" "7687" "Neo4j (Bolt)"
check_tcp "localhost" "6333" "Qdrant (HTTP)"
check_tcp "localhost" "6334" "Qdrant (gRPC)"
echo ""

# --- Top-hub insight (knowledge-rag pattern) ---
echo "--- Knowledge Graph Top Hub ---"
if command -v knowledge-rag >/dev/null 2>&1; then
  echo "Running knowledge-rag top-hub query..."
  knowledge-rag top-hub 2>/dev/null || echo "⚠️  knowledge-rag top-hub failed or not configured"
else
  echo "⚠️  knowledge-rag CLI not found (install/configure to enable top-hub insights)"
fi
echo ""

# --- Build/update asset index ---
echo "--- Asset Index ---"
build_index() {
  local idx="{\"generated_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"services\":{},\"datasets\":{},\"repos\":{}}"

  idx="$(echo "$idx" | jq '.services.arkship_ui = {port:3000, url:"http://localhost:3000"}')"
  idx="$(echo "$idx" | jq '.services.arkship_api = {port:8000, url:"http://localhost:8000"}')"
  idx="$(echo "$idx" | jq '.services.surrogate_ai = {port:8001, url:"http://localhost:8001"}')"

  for base in "arkship/data" "surrogate/data"; do
    d="${PROJECT_ROOT}/${base}"
    if [ -d "$d" ]; then
      while IFS= read -r f; do
        rel="${base}/$(basename "$f")"
        sz="$(du -h "$f" 2>/dev/null | cut -f1 || echo "?")"
        idx="$(echo "$idx" | jq --arg p "$rel" --arg s "$sz" '.datasets[$p] = {size:$s}')"
      done < <(find "$d" -maxdepth 1 -type f ! -name '.*' 2>/dev/null | sort)
    fi
  done

  for sub in arkship surrogate; do
    if [ -d "${PROJECT_ROOT}/${sub}" ]; then
      commit="$(git -C "${PROJECT_ROOT}/${sub}" rev-parse HEAD 2>/dev/null || echo 'unknown')"
      branch="$(git -C "${PROJECT_ROOT}/${sub}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
      idx="$(echo "$idx" | jq --arg b "$branch" --arg c "$commit" '.repos[$sub] = {branch:$b, commit:$c}')"
    fi
  done

  echo "$idx"
}

if [ ! -f "${INDEX_FILE}" ] || [ "${UPDATE_INDEX}" -eq 1 ]; then
  echo "Generating asset index..."
  build_index > "${INDEX_FILE}"
  echo "✅ Index written to ${INDEX_FILE}"
else
  echo "📄 Using existing index: ${INDEX_FILE} (use --update-index to refresh)"
fi

echo ""
echo "--- Index Summary ---"
jq '{services, repos, dataset_count: (.datasets | length)}' "${INDEX_FILE}"

echo ""
echo "--- Next Actions ---"
echo "• Review datasets: jq '.datasets' ${INDEX_FILE}"
echo "• Pre-list HF folder for CDN training: list_repo_tree(...) -> file-list.json (embed in train.py)"
echo "• Reuse running Lightning Studio to save quota (see patterns)"
echo "• If training surrogate-1: avoid streaming=True for mixed-schema repos; use hf_hub_download per file"
```

Make executable:
```bash
chmod +x scripts/discover.sh
```

---

### `arkship/api/discovery.py`
Machine-readable topology and health from Arkship.

```python
from fastapi import APIRouter, HTTPException
import httpx
import os
from typing import Dict, Any

router = APIRouter(prefix="/meta", tags=["meta"])

SURROGATE_URL = os.getenv("SURROGATE_URL", "http://localhost:8001")
NEO4J_HOST = os.getenv("NEO4J_HOST", "localhost")
NEO4J_PORT = int(os.getenv("NEO4J_BOLT_PORT", "7687"))
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

async def check_http(url: str, timeout: float = 2.0) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}

async def check_tcp(host: str, port: int, timeout: float = 2.0) -> Dict[str, Any]:
    import asyncio
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return {"reachable": True}
    except Exception as exc:
        return {"reachable": False,
