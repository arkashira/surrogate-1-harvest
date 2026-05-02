# airship / discovery

## Final synthesized implementation

**Chosen approach**: merge Candidate 1’s concrete, executable tooling with Candidate 2’s correct scoping (place discovery in `surrogate/`), and resolve contradictions in favor of correctness + immediate actionability.

- **Contradiction resolved (location)**: Candidate 1 proposed `bin/discover` at repo root; Candidate 2 correctly scopes discovery to the Surrogate service (which owns the Knowledge Graph/Neo4j and Vector Store/Qdrant). Final: `surrogate/bin/discover`.
- **Contradiction resolved (capabilities)**: combine service topology + health checks (Candidate 1) with explicit knowledge-rag/top-hub queries and market-research hooks (Candidate 2) into a single, fast, safe utility.
- **Contradiction resolved (safety)**: keep cron-safe wrapper pattern (Candidate 1) and HF CDN training stub (Candidate 1) but place them under `surrogate/scripts/` for consistency.

---

### 1) Create `surrogate/bin/discover` (executable)

```bash
#!/usr/bin/env bash
# surrogate/bin/discover — quick onboarding + discovery for Surrogate stack
# Usage: ./surrogate/bin/discover
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "== Surrogate Discovery =="
echo "Project root: $BASE"
echo ""

# Service topology (microservices)
echo "--- Services (docker-compose.microservices.yml) ---"
COMPOSE_FILE="$BASE/docker-compose.microservices.yml"
if command -v docker compose >/dev/null 2>&1 && [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || \
    echo "Unable to list containers (are they running?). Try: docker compose -f $COMPOSE_FILE up -d"
else
  echo "Missing compose file or docker compose CLI."
fi
echo ""

# Health checks (fast, local)
echo "--- Health checks ---"
for svc in "Arkship:8000" "Surrogate:8001" "UI:3000"; do
  name="${svc%%:*}"
  port="${svc##*:}"
  if curl -s -f -m 2 "http://localhost:${port}/health" >/dev/null 2>&1 || curl -s -f -m 2 "http://localhost:${port}/" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ $name (port $port) responding${NC}"
  else
    echo -e "${YELLOW}✗ $name (port $port) not reachable${NC}"
  fi
done
echo ""

# Knowledge Graph: top hub + recent artifacts (knowledge-rag pattern)
echo "--- Knowledge Graph (top hub) ---"
if command -v python3 >/dev/null 2>&1; then
  python3 -c "
import os, sys, json, subprocess, shlex, datetime

def run(cmd):
    return subprocess.check_output(shlex.split(cmd), text=True, stderr=subprocess.DEVNULL).strip()

# Prefer explicit knowledge-rag CLI if available
try:
    out = run('knowledge-rag query --hub MOC --depth 2 --limit 5')
    print(out)
except Exception:
    # Fallback stub
    print('Top hub: MOC (most-connected)')
    print('Recent artifacts (stub):')
    for item in ['MOC-2024-06-12', 'MOC-2024-06-10', 'MOC-2024-06-08']:
        print(f'  - {item}')
    print('')
    print('To run full query (if installed):')
    print('  knowledge-rag query --hub MOC --depth 2')
" 2>/dev/null || true
else
  echo "Python not available — install to query knowledge graph."
fi
echo ""

# Market research hook (business-research pattern)
echo "--- Business research (pattern) ---"
echo "Run: granite-business-research.sh && knowledge-rag query --top-hubs"
echo ""

# HF CDN training scaffold
echo "--- Training (HF CDN bypass) ---"
echo "See: surrogate/scripts/train_cdn.py"
echo ""

echo "Done."
```

Make executable:
```bash
chmod +x /opt/axentx/airship/surrogate/bin/discover
```

---

### 2) Add `surrogate/scripts/train_cdn.py` (HF CDN bypass stub)

```python
"""
HF CDN bypass training stub (Surrogate pattern).

Steps:
1) On Mac (or any dev machine), list repo tree once (after rate-limit window):
   python -c "from huggingface_hub import list_repo_tree; import json; files=list_repo_tree('datasets/username/repo', recursive=False); print(json.dumps(files, indent=2))" > file_list.json
2) Embed file_list.json in this script (or pass path).
3) Lightning Studio training uses CDN-only fetches (zero API calls during data load).
"""
import json
import os
import argparse
from pathlib import Path
from typing import List

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/username/repo")
HF_HUB_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def load_file_list(list_path: str) -> List[str]:
    """Load pre-listed file paths (non-recursive, one folder)."""
    with open(list_path) as f:
        data = json.load(f)
    return [item["path"] for item in data if item.get("path")]

def cdn_urls(file_paths: List[str]) -> List[str]:
    return [HF_HUB_CDN_TEMPLATE.format(repo=HF_DATASET_REPO, path=p) for p in file_paths]

def project_to_prompt_response(file_path: str):
    """
    Download one file via CDN and project to {prompt, response}.
    Replace with real parser for your repo format.
    """
    import requests
    url = HF_HUB_CDN_TEMPLATE.format(repo=HF_DATASET_REPO, path=file_path)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    # Example: assume JSON lines with 'instruction' and 'output'
    return {"prompt": f"Process: {file_path}", "response": "stub"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", default="file_list.json")
    parser.add_argument("--out", default="samples.jsonl")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if not Path(args.list).exists():
        print(f"File list not found: {args.list}")
        print("Create it with list_repo_tree (non-recursive) and re-run.")
        exit(1)

    paths = load_file_list(args.list)[:args.limit]
    with open(args.out, "w") as f:
        for p in paths:
            sample = project_to_prompt_response(p)
            f.write(json.dumps(sample) + "\n")
    print(f"Wrote {len(paths)} samples to {args.out}")
```

---

### 3) Add `surrogate/scripts/run_wrapper.sh` (cron-safe)

```bash
#!/usr/bin/env bash
# Cron-safe wrapper template (see patterns: opus-pr-reviewer, active-learning).
# Usage in crontab:
#   SHELL=/bin/bash
#   * * * * * /opt/axentx/airship/surrogate/scripts/run_wrapper.sh >> /var/log/airship-wrapper.log 2>&1

set -euo pipefail
export SHELL=/bin/bash
cd "$(dirname "$0")/.."

# Replace the line below with the real command
echo "$(date -Iseconds): wrapper running"
# Example: python scripts/...
```

Make executable:
```bash
chmod +x /opt/axentx/airship/surrogate/scripts/run_wrapper.sh
```

---

### 4) Add Makefile target (optional, convenience)

If you want a top-level `make discover`, add to `Makefile` (create if absent):


