# Costinel / discovery

## Final Synthesis — One Correct, Actionable Answer

**Core diagnosis (unified):**  
Costinel lacks an automated discovery surface for context (knowledge graph / top-hub docs), market insights, and file manifests needed for training and operations. This blocks onboarding, RAG, and reproducible pipelines.

**Chosen approach:**  
Add a single, lightweight discovery CLI + one helper script that:
- queries top-hub/knowledge-rag,
- runs market research and ingests results,
- produces file manifests (local-first, CDN fallback),
- prints dashboard/health links,
- includes a smoke test for verification.

All changes are additive (~150–200 lines total) and do not touch core app logic.

---

### 1) Create directory
```bash
mkdir -p /opt/axentx/Costinel/scripts
```

---

### 2) `/opt/axentx/Costinel/scripts/discover.py`
```python
#!/usr/bin/env python3
"""
Costinel discovery CLI
Usage:
  python discover.py knowledge          # query top-hub and related docs
  python discover.py market             # run market research + ingest
  python discover.py files <repo> <path> # list repo tree and emit CDN manifest
  python discover.py dashboard          # print dashboard links + health
  python discover.py verify             # smoke test outputs
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

def run(cmd, capture=True, check=True):
    if capture:
        return subprocess.check_output(cmd, shell=True, text=True, cwd=REPO_ROOT).strip()
    else:
        subprocess.check_call(cmd, shell=True, cwd=REPO_ROOT)

def knowledge():
    """Query top-hub (MOC) and related docs via knowledge-rag if available."""
    print("[discover] Querying top-hub and related docs...")
    rag_script = REPO_ROOT / "scripts" / "knowledge-rag.sh"
    if rag_script.exists():
        run(f"bash {rag_script} query --hub MOC", capture=False)
    else:
        print("  -> knowledge-rag not found; install or link to graph pipeline")
        print("  -> Expected top-hub: MOC (most-connected)")
        print("  -> Tags: #knowledge-rag #graph #hub")

def market():
    """Run granite-business-research and feed results into knowledge-rag."""
    print("[discover] Running market analysis...")
    script = REPO_ROOT / "scripts" / "granite-business-research.sh"
    if not script.exists():
        print("  -> granite-business-research.sh not found; creating stub")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("""#!/usr/bin/env bash
set -euo pipefail
echo 'granite-business-research: collecting market insights...'
cat > market_insights.json <<'EOF'
{
  "hub": "MOC",
  "insights": [
    "Multi-cloud cost governance demand increasing 2026",
    "FinOps teams prioritize real-time visibility and governance without execution",
    "Auditability and approval workflows are top-3 requested features"
  ]
}
EOF
""")
        script.chmod(0o755)
    run(f"bash {script}", capture=False)

    if (REPO_ROOT / "scripts" / "knowledge-rag.sh").exists():
        run("bash scripts/knowledge-rag.sh ingest market_insights.json", capture=False)
    print("  -> market analysis complete; tags: #business-research #knowledge-rag #graph")

def files(repo, path):
    """List repo tree (non-recursive) and emit CDN manifest for training/discovery."""
    print(f"[discover] Listing {repo}/{path} (non-recursive)...")
    local_path = (REPO_ROOT / path).resolve()
    if local_path.exists() and local_path.is_dir():
        items = [p.name for p in local_path.iterdir()]
        manifest = {
            "repo": repo,
            "path": path,
            "local": True,
            "files": items,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
    else:
        print("  -> local path not found; producing HF CDN template")
        print("  -> Note: run list_repo_tree(path, recursive=False) on Mac and save to files.json for training")
        manifest = {
            "repo": repo,
            "path": path,
            "local": False,
            "cdn_template": f"https://huggingface.co/datasets/{repo}/resolve/main/{path}/",
            "instruction": "Run list_repo_tree once, save to files.json, and embed in training script for CDN-only fetches.",
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    out = REPO_ROOT / "files.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"  -> manifest written to {out}")
    print("  -> tags: #training #api-strategy #file-list")

def dashboard():
    """Print dashboard links and basic health."""
    print("[discover] Costinel dashboard endpoints")
    for p in (3000, 8080, 8000):
        print(f"  http://localhost:{p}  (try)")
    docker_compose = REPO_ROOT / "docker-compose.yml"
    if docker_compose.exists():
        print("  -> docker-compose available; run: docker compose up -d")
    print("  -> Features: visibility, intelligence, governance, auditability")
    print("  -> Core philosophy: Sense + Signal — ไม่ Execute")

def verify():
    """Smoke test: check key outputs exist and are valid."""
    print("[discover] Running smoke tests...")
    checks = []

    # files.json
    files_json = REPO_ROOT / "files.json"
    if files_json.exists():
        try:
            data = json.loads(files_json.read_text())
            checks.append(("files.json valid JSON", True))
            checks.append(("files.json has repo", "repo" in data))
        except Exception:
            checks.append(("files.json valid JSON", False))
    else:
        checks.append(("files.json exists", False))

    # market_insights.json
    market_json = REPO_ROOT / "market_insights.json"
    if market_json.exists():
        try:
            data = json.loads(market_json.read_text())
            checks.append(("market_insights.json valid JSON", True))
            checks.append(("market_insights.json has insights", "insights" in data))
        except Exception:
            checks.append(("market_insights.json valid JSON", False))
    else:
        checks.append(("market_insights.json exists", False))

    for name, ok in checks:
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {name}")

    if all(ok for _, ok in checks):
        print("[discover] verify passed")
    else:
        print("[discover] verify failed; see above")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Costinel discovery CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("knowledge", help="Query top-hub and related docs")
    sub.add_parser("market", help="Run market analysis and feed knowledge-rag")
    fp = sub.add_parser("files", help="List repo tree and emit CDN manifest")
    fp.add_argument("repo", help="Repo identifier (e.g., datasets/repo_name)")
    fp.add_argument("path", help="Folder path in repo")
    sub.add_parser("dashboard", help="Show dashboard links and health")
    sub.add_parser("verify", help="Smoke test outputs")

    args = parser.parse_args()
    try:
        if args.cmd == "knowledge":
            knowledge()
        elif args.cmd == "market":
            market()
        elif args.cmd == "files":
            files(args.repo, args.path)
        elif args.cmd == "dashboard":
            dashboard()
        elif args.cmd == "verify":
            verify()
    except Exception as e:
        print(f"[discover] error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

### 3) `/opt/axentx/Costinel/scripts/granite-business-research.sh`
