# Costinel / discovery

## Incremental Improvement: Add Discovery Surface (Manifest + Health Probe + Top-Hub Insight)

**Value:** Unblock onboarding, RAG queries, and training ingestion by exposing a machine-readable manifest, readiness probe, and curated top-hub insight — all achievable in <2h.

---

### Implementation Plan

1. **Create `discovery/` module**  
   - `discovery/manifest.json` — machine-readable sources (cloud accounts, datasets, schemas, HF repos, date folders).  
   - `discovery/health.py` — readiness/liveness probe (fast, no external deps except optional checks).  
   - `discovery/top_hub.md` — curated top-hub insight (MOC) pulled from knowledge-rag context.

2. **Add lightweight CLI entrypoint**  
   - `scripts/discover.sh` — prints manifest + health status; usable in CI and local dev.

3. **Wire into existing docs**  
   - Update `README.md` with “Discovery” section linking to `discovery/` outputs.

4. **Ensure patterns compliance**  
   - Use CDN bypass pattern for HF file lists (embed JSON).  
   - Avoid heavy compute on Mac; keep CLI orchestration-only.

---

### Code Snippets

#### `discovery/manifest.json`
```json
{
  "version": "1.0.0",
  "generated_at": "2026-05-02T19:00:00Z",
  "sources": {
    "cloud_accounts": [
      {
        "provider": "aws",
        "account_id": "123456789012",
        "regions": ["us-east-1", "eu-west-1"],
        "tags": ["prod", "finance"]
      },
      {
        "provider": "gcp",
        "project_id": "costinel-prod",
        "regions": ["us-central1", "europe-west1"]
      }
    ],
    "datasets": {
      "surrogate_1": {
        "repo": "AXENTX/costinel-ingest",
        "date_folders": [
          "2026-04-27",
          "2026-04-28",
          "2026-04-29"
        ],
        "file_manifest": "2026-04-29_files.json",
        "cdn_prefix": "https://huggingface.co/datasets/AXENTX/costinel-ingest/resolve/main"
      }
    },
    "knowledge_graph": {
      "top_hub": "MOC",
      "hub_doc": "knowledge-rag/top-hub-MOC.md"
    }
  }
}
```

#### `discovery/health.py`
```python
#!/usr/bin/env python3
"""
Lightweight readiness/liveness probe for Costinel discovery surface.
Exit 0 = healthy, non-zero = unhealthy.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST_PATH = ROOT / "discovery" / "manifest.json"

def check_manifest() -> bool:
    if not MANIFEST_PATH.exists():
        print("ERROR: manifest.json missing", file=sys.stderr)
        return False
    try:
        with MANIFEST_PATH.open() as f:
            data = json.load(f)
        required = ["version", "sources"]
        if not all(k in data for k in required):
            print("ERROR: manifest.json missing required keys", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"ERROR: invalid manifest.json: {exc}", file=sys.stderr)
        return False

def check_top_hub() -> bool:
    hub_doc = ROOT / "discovery" / "top_hub.md"
    if not hub_doc.exists():
        print("WARN: top_hub.md missing (non-blocking)", file=sys.stderr)
        return True  # non-blocking
    return True

def main() -> None:
    checks = [check_manifest, check_top_hub]
    ok = all(fn() for fn in checks)
    if ok:
        print("OK: discovery surface healthy")
        sys.exit(0)
    else:
        print("FAIL: discovery surface unhealthy", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

#### `discovery/top_hub.md`
```markdown
# Top-Hub Insight: MOC

**Source:** knowledge-rag graph (2026-04-27)  
**Relevance:** Most-connected hub for Costinel context and decision patterns.

## Summary
MOC (Method of Choice) hub aggregates governance playbooks, cost optimization patterns, and approval workflows used across AXENTX cloud estates.

## Key Signals
- Prefer CDN-bypass for HF dataset ingestion to avoid API rate limits.
- Use deterministic repo hashing for HF commit cap mitigation.
- Lightning Studio reuse to preserve quota; avoid idle-stop training loss.
- Separate orchestration (Mac) from heavy compute (Lightning/Kaggle).

## Actionable for Costinel
- Wire MOC signals into recommendation engine to surface reserved instance and idle-resource proposals.
- Embed file manifest (date folder) in surrogate-1 training scripts to enable zero-API CDN fetches.
```

#### `scripts/discover.sh`
```bash
#!/usr/bin/env bash
# Usage: ./scripts/discover.sh [--health|--manifest|--all]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MANIFEST="$REPO_ROOT/discovery/manifest.json"
HEALTH_SCRIPT="$REPO_ROOT/discovery/health.py"

show_manifest() {
  if [[ -f "$MANIFEST" ]]; then
    cat "$MANIFEST"
  else
    echo "ERROR: manifest not found at $MANIFEST" >&2
    exit 1
  fi
}

show_health() {
  if [[ -x "$HEALTH_SCRIPT" ]]; then
    python3 "$HEALTH_SCRIPT"
  else
    echo "ERROR: health script not executable or missing" >&2
    exit 1
  fi
}

case "${1:---all}" in
  --manifest) show_manifest ;;
  --health)   show_health ;;
  --all)      show_manifest; echo "---"; show_health ;;
  *)          echo "Usage: $0 [--health|--manifest|--all]" >&2; exit 1 ;;
esac
```

#### Update `README.md` (append section)
```markdown
## 🔍 Discovery Surface

Machine-readable manifest and health probes to accelerate onboarding and automation.

- **Manifest:** [`discovery/manifest.json`](discovery/manifest.json) — cloud accounts, datasets, HF sources, and graph hubs.
- **Health probe:** `python3 discovery/health.py` or `scripts/discover.sh --health`
- **Top-hub insight:** [`discovery/top_hub.md`](discovery/top_hub.md) — MOC hub context for governance patterns.

Quick CLI:
```bash
# Show manifest and health
bash scripts/discover.sh --all
```
```

---

### Execution Steps (under 2h)

1. Create `discovery/` directory and add the three files above.  
2. Add `scripts/discover.sh` and `chmod +x scripts/discover.sh`.  
3. Update `README.md` with the new section.  
4. Run `bash scripts/discover.sh --all` to verify.  
5. Commit and push.

No heavy compute, no external API calls during runtime, and fully compliant with known patterns (CDN bypass, top-hub review, orchestration-only on Mac).
