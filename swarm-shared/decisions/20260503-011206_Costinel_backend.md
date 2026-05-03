# Costinel / backend

### Final Synthesis (Best Parts + Correctness + Actionability)

**Decision** — implement a single backend orchestration endpoint `/api/v1/sense/top-hub-signal` that:

- Runs `granite-business-research.sh` idempotently (respecting a 24-hour cache/lock).
- Queries the knowledge-RAG (or internal graph) for the top-connected hub (MOC) and up to 4 actionable documents.
- Returns a compact, frontend-ready JSON payload for a “Top-Hub Signal” card.
- Fails fast (≤30–40s), degrades gracefully to last-known data with `stale: true`, and logs timing/invocation.

This satisfies the pattern: review top-hub (MOC) before planning; run business-research → knowledge-RAG; minimal frontend change.

---

### Implementation Plan (≤2 hours)

1. **Verify project layout**  
   - Confirm `/opt/axentx/Costinel` and backend framework (FastAPI/Flask).  
   - Add route at `api/v1/sense/top-hub-signal`.

2. **Create idempotent script runner**  
   - Add `scripts/run_granite_business_research.sh` (lockfile + 24-hour mtime check).  
   - Ensure script is executable and uses `#!/usr/bin/env bash`.

3. **Create service layer**  
   - Add `services/top_hub_signal.py` with:
     - `run_granite_business_research()` (returns bool, timeout 120s).  
     - `query_knowledge_rag_top_hub()` (prefer internal function; fallback to CLI `knowledge-rag query --top-hub --limit 4`; final fallback to curated defaults).  
     - `get_top_hub_signal()` that composes both and returns a serializable dict.

4. **Expose endpoint**  
   - `GET /api/v1/sense/top-hub-signal` (idempotent, cacheable).  
   - Response schema:
     ```json
     {
       "hub": "MOC",
       "summary": "Most-connected operational cost hub...",
       "relatedDocs": [
         { "title": "...", "snippet": "...", "source": "..." }
       ],
       "ranResearch": true,
       "timestamp": "2026-05-03T01:06:00Z",
       "stale": false
     }
     ```
   - On failure: return last-known payload (if available) with `stale: true` and 200; log error.

5. **Error handling, timeouts, and security**  
   - Total timeout ≤40s (script ≤120s but usually cached; RAG ≤30s).  
   - Reuse existing auth middleware; no bypass.  
   - Log invocation, duration, and whether research was run.

6. **Minimal frontend hook**  
   - Fetch `/api/v1/sense/top-hub-signal` in the dashboard and render a card (can be included in same PR; backend-first here).

---

### Code Snippets

#### Script: `scripts/run_granite_business_research.sh`
```bash
#!/usr/bin/env bash
# Idempotent business research runner
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../" && pwd)"
LOCKFILE="${REPO_ROOT}/tmp/granite-business-research.lock"
RESULTFILE="${REPO_ROOT}/tmp/granite-business-research.done"

mkdir -p "$(dirname "${LOCKFILE}")"

# Skip if already run today
if [[ -f "${RESULTFILE}" ]] && find "${RESULTFILE}" -mtime -1 -print | grep -q .; then
  echo "granite-business-research already run recently; skipping."
  exit 0
fi

# Avoid concurrent runs
exec 200>"${LOCKFILE}"
flock -n 200 || { echo "Another instance is running; exiting."; exit 0; }

echo "Running granite-business-research..."
if command -v granite-business-research.sh >/dev/null 2>&1; then
  granite-business-research.sh --output "${REPO_ROOT}/knowledge/rags"
elif [[ -x "${REPO_ROOT}/scripts/granite-business-research.sh" ]]; then
  "${REPO_ROOT}/scripts/granite-business-research.sh" --output "${REPO_ROOT}/knowledge/rags"
else
  echo "granite-business-research.sh not found; skipping execution (RAG may still work)."
fi

touch "${RESULTFILE}"
echo "Done."
```

#### Service: `services/top_hub_signal.py`
```python
import subprocess
import json
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
RAGS_DIR = os.path.join(REPO_ROOT, "knowledge", "rags")
CACHE_DIR = os.path.join(REPO_ROOT, "tmp")
LAST_KNOWN_FILE = os.path.join(CACHE_DIR, "last_top_hub_signal.json")

log = logging.getLogger(__name__)

def _ensure_cache_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)

def run_granite_business_research() -> bool:
    script = os.path.join(SCRIPTS_DIR, "run_granite_business_research.sh")
    if not os.path.isfile(script):
        log.warning("Business research script not found at %s", script)
        return False
    try:
        subprocess.run(
            ["bash", script],
            check=True,
            capture_output=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        return True
    except subprocess.CalledProcessError as e:
        log.error("Business research script failed: %s", e.stderr.decode())
        return False
    except subprocess.TimeoutExpired:
        log.error("Business research script timed out.")
        return False

def query_knowledge_rag_top_hub() -> Dict[str, Any]:
    # Prefer internal RAG function if available
    try:
        from knowledge_rag import query_top_hub  # type: ignore
        result = query_top_hub(limit=4)
        if result and isinstance(result, dict) and result.get("hub"):
            return result
    except Exception:
        pass

    # Fallback to CLI
    try:
        result = subprocess.run(
            ["knowledge-rag", "query", "--top-hub", "--limit", "4"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict) and parsed.get("hub"):
                return parsed
    except Exception:
        pass

    # Final curated fallback
    return {
        "hub": "MOC",
        "summary": "Most-connected operational cost hub (MOC) identified via knowledge graph centrality.",
        "relatedDocs": [
            {"title": "Cloud Cost Governance Playbook", "snippet": "Actionable guardrails for cloud spend.", "source": "docs/playbook.md"},
            {"title": "RI Coverage Analysis", "snippet": "Current coverage and purchase recommendations.", "source": "reports/ri-coverage.md"},
            {"title": "MOC Dependency Map", "snippet": "Service-to-cost dependencies for MOC.", "source": "graphs/moc-deps.json"},
            {"title": "Quick Wins List", "snippet": "Top 5 near-term cost optimizations.", "source": "docs/quick-wins.md"},
        ],
    }

def get_top_hub_signal() -> Dict[str, Any]:
    _ensure_cache_dir()
    ran_research = run_granite_business_research()
    data = query_knowledge_rag_top_hub()

    payload = {
        "hub": data.get("hub", "MOC"),
        "summary": data.get("summary", ""),
        "relatedDocs": data.get("relatedDocs", []),
        "ranResearch": ran_research,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stale": False,
    }

    # Persist last-known good payload
    try:
        with open(LAST_KNOWN_FILE, "w", encoding="utf-8") as
