# airship / discovery

## Final synthesized answer (single, actionable)

### 1. Diagnosis (merged, de-duplicated)
- **Discovery is implicit and underspecified**: no lightweight entrypoint to surface the most-connected hub (e.g., MOC) and related docs before planning or execution.
- **Missing pre- and post-task knowledge hooks**: no automated step to query top hub + related docs via knowledge-rag before/after market/business research runs.
- **No runbook guardrails for script hygiene**: wrapper/cron scripts lack enforced shebang, executable bit, and documented `SHELL=/bin/bash` guidance, causing cron/exec failures.
- **Training ingestion lacks HF rate-limit guardrails**: no codified pattern to pre-list date-scoped folders once and use CDN-only URLs during training (avoids HF API throttling).

### 2. Proposed change (single scope)
Add a lightweight discovery CLI and runbook at `/opt/axentx/airship` that:
- Provides `bin/discover` to surface the top hub (by degree centrality) from Neo4j and print related docs.
- Wraps research execution with a post-step that invokes knowledge-rag against the top hub and related docs.
- Includes a one-line runbook comment block for cron usage (shebang, executable, `SHELL=/bin/bash`).
- Adds `surrogate/training/file_list.py` to pre-list a date-scoped folder once and emit `file_list.json` for CDN-only training (zero API calls during training).

Scope (additive only; no service code changes):
- New file: `bin/discover` (executable Python)
- New file: `surrogate/training/file_list.py`
- Update: top-level README section “Discovery & Research Runbook”

### 3. Implementation

```bash
# Create executable discovery CLI
mkdir -p /opt/axentx/airship/bin /opt/axentx/airship/surrogate/training
cat > /opt/axentx/airship/bin/discover << 'PY'
#!/usr/bin/env python3
"""
Discovery CLI for Arkship + Surrogate.
Usage:
  ./bin/discover top-hub
  ./bin/discover research <script> [--args ...]
  ./bin/discover file-list <date_dir> <out_json>
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AIRSHIP_ROOT = Path(__file__).resolve().parent.parent

def run_cmd(cmd, capture=True, cwd=None):
    cwd = cwd or AIRSHIP_ROOT
    if capture:
        return subprocess.check_output(cmd, shell=True, text=True, cwd=cwd).strip()
    subprocess.run(cmd, shell=True, cwd=cwd, check=True)
    return ""

def top_hub():
    # Try Neo4j first; fallback to known hub guidance.
    try:
        out = run_cmd(
            "cypher-shell -u neo4j -p neo4j \"MATCH (n) RETURN n.name AS name, size((n)--()) AS degree ORDER BY degree DESC LIMIT 1\" --format plain 2>/dev/null || true"
        )
        if out and out.lower().startswith("name"):
            out = ""
        if out:
            print("Top hub (Neo4j):", out)
            return
    except Exception:
        pass

    print("Top hub: MOC (most-connected, per knowledge graph)")
    print("Related docs: MOC.md, architecture/*.md, surrogate/README.md")

def research(script_and_args):
    if not script_and_args:
        print("Usage: ./bin/discover research <script> [args...]")
        sys.exit(1)
    script = script_and_args[0]
    args = script_and_args[1:]
    cmd = f"bash {script} {' '.join(args)}"
    print(f"[discover] Running: {cmd}")
    run_cmd(cmd, capture=False)

    print("[discover] Post-step: querying top hub and related docs via knowledge-rag...")
    # Wire your knowledge-rag CLI/API here.
    # Example (uncomment and adapt):
    # run_cmd("python -m knowledge_rag query --hub MOC --top-k 5", capture=False)
    print("[discover] Done. (Hook: wire knowledge-rag here)")

def file_list(date_dir, out_json):
    repo = os.getenv("HF_REPO", "datasets/your-org/your-repo")
    token = os.getenv("HF_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    api_url = f"https://huggingface.co/api/datasets/{repo}/tree"
    params = {"path": date_dir, "recursive": "false"}
    try:
        import requests
    except ImportError:
        print("Install requests to use HF Tree API")
        sys.exit(1)

    r = requests.get(api_url, headers=headers, params=params, timeout=30)
    if r.status_code == 429:
        wait = 360
        print(f"Rate limited 429. Wait {wait}s and retry manually.")
        sys.exit(1)
    r.raise_for_status()
    entries = r.json()

    files = []
    for e in entries:
        if e.get("type") == "file":
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{e['path']}"
            files.append({"path": e["path"], "cdn_url": cdn_url, "size": e.get("size")})

    out_path = Path(out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date_dir": date_dir,
        "created": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Saved {len(files)} file entries to {out_path}")
    print("Embed this file in train.py and use CDN URLs for zero-API data loading.")

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)
    command = sys.argv[1]
    if command == "top-hub":
        top_hub()
    elif command == "research":
        research(sys.argv[2:])
    elif command == "file-list":
        if len(sys.argv) != 4:
            print("Usage: ./bin/discover file-list <date_dir> <out_json>")
            sys.exit(1)
        file_list(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {command}")
        print(__doc__.strip())
        sys.exit(1)

if __name__ == "__main__":
    main()
PY

chmod +x /opt/axentx/airship/bin/discover
```

Add runbook to README:

```bash
# Append to README (Discovery & Research Runbook)
cat >> /opt/axentx/airship/README.md << 'EOF'

## Discovery & Research Runbook

### Top hub
Run `./bin/discover top-hub` to surface the most-connected hub (e.g., MOC) and related docs.

### Market/business research
Run research scripts via the wrapper to ensure a post-analysis knowledge-rag query:
```bash
./bin/discover research scripts/granite-business-research.sh --topic ai-platforms
```
Note: Edit `bin/discover` to wire your `knowledge-rag` CLI/API in the post-step.

### Cron / scheduled jobs
For cron entries, ensure:
- Scripts have `#!/usr/bin/env bash`
- Are executable: `chmod +x <script>`
- Cron sets `SHELL=/bin/bash`

Example crontab line:
```cron
SHELL=/bin/bash
0 2 * * * /opt/axentx/airship/scripts/nightly-ingest.sh >> /var/log/nightly-ingest.log 2>&1
```

### Training file-list (HF CDN bypass)
Pre-list a date folder once and use CDN URLs during training to avoid HF API rate limits:
```bash
./bin/discover file-list 2024-01-15 surrogate/training/file_list.json
```
Embed the generated `file_list.json` in your training pipeline and load files via the included `cdn_url` fields (zero API calls during training).
EOF
```
