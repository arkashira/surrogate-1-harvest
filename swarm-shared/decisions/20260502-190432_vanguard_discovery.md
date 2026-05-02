# vanguard / discovery

## Final Synthesis (single authoritative answer)

**Core problem**: discovery is manual, brittle, and non-reusable; HF ingestion will 429, Studio quota is wasted, and schema drift will break surrogate-1 training.

**Single solution**: one executable discovery command that (1) queries top-hub via knowledge-rag, (2) produces a CDN-bypass HF file list, (3) reuses or restarts a Lightning Studio (never blind create), and (4) projects local raw data into strict `{prompt,response}` parquet.

---

## 1) Canonical entrypoint (executable)

`/opt/axentx/vanguard/discovery/run_discovery.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
SHELL=/bin/bash

VANGUARD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISCOVERY_ROOT="${VANGUARD_ROOT}/discovery"
RAW_ROOT="${VANGUARD_ROOT}/data/raw"
OUT_ROOT="${VANGUARD_ROOT}/data/batches/mirror-merged"
HF_REPO="${HF_DATASET_REPO:-datasets/company-knowledge}"
HF_DATE="${HF_DATE:-$(date +%Y-%m-%d)}"
HF_OUT="${DISCOVERY_ROOT}/hf_file_list_${HF_DATE}.json"
STUDIO_NAME="${STUDIO_NAME:-vanguard-train-studio}"
MACHINE="${MACHINE:-L40S}"

log() { echo "[$(date -Iseconds)] $*"; }

main() {
  log "Starting vanguard discovery run"

  # 1) Top-hub insight (MOC) via knowledge-rag (best-effort)
  if command -v knowledge-rag &>/dev/null; then
    log "Querying top-hub (MOC) insights via knowledge-rag"
    knowledge-rag query --top-hub MOC --limit 5 --out "${DISCOVERY_ROOT}/top_hub_moc.json" || \
      log "knowledge-rag query failed — continuing"
  else
    log "knowledge-rag not installed — skipping top-hub query"
  fi

  # 2) HF CDN-bypass file list (single API call, then CDN-only)
  log "Generating HF CDN-bypass file list for ${HF_REPO} on ${HF_DATE}"
  python3 -m pip show huggingface_hub &>/dev/null || {
    log "Installing huggingface_hub (required for HF listing)"
    python3 -m pip install --quiet huggingface_hub
  }
  python3 "${DISCOVERY_ROOT}/list_hf_files.py" \
    --repo "${HF_REPO}" \
    --date "${HF_DATE}" \
    --out "${HF_OUT}"

  # 3) Reuse or restart/create Lightning Studio (quota-safe)
  log "Ensuring Lightning Studio '${STUDIO_NAME}' (reuse > restart > create)"
  python3 "${DISCOVERY_ROOT}/ensure_studio.py" \
    --name "${STUDIO_NAME}" \
    --machine "${MACHINE}" \
    --out-state "${DISCOVERY_ROOT}/studio_state.json"

  # 4) Project local raw data to canonical schema (strict prompt/response)
  if [[ -d "${RAW_ROOT}" ]] && [[ -n "$(ls -A "${RAW_ROOT}" 2>/dev/null || true)" ]]; then
    log "Projecting local raw data to canonical schema"
    python3 "${DISCOVERY_ROOT}/project_local_to_canonical.py" \
      --input-root "${RAW_ROOT}" \
      --date "${HF_DATE}" \
      --out-root "${OUT_ROOT}"
  else
    log "No local raw data found at ${RAW_ROOT} — skipping projection"
  fi

  log "Discovery run complete"
  log "HF file list: ${HF_OUT}"
  log "Studio state: ${DISCOVERY_ROOT}/studio_state.json"
  log "Canonical data: ${OUT_ROOT}/${HF_DATE}/"
}

main "$@"
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/discovery/run_discovery.sh
```

---

## 2) HF CDN-bypass file list (rate-limit-safe)

`/opt/axentx/vanguard/discovery/list_hf_files.py`

```python
#!/usr/bin/env python3
"""
Single-call HF tree lister for a date folder.
Output: JSON with repo, date, files, and CDN-ready URLs (no auth for public datasets).
"""
import argparse
import json
import os
import sys
from typing import List, Dict, Any

try:
    from huggingface_hub import HfApi
except ImportError as e:
    sys.stderr.write("ERROR: huggingface_hub not installed. Install via: pip install huggingface_hub\n")
    sys.exit(1)

def list_hf_files(repo: str, date: str) -> Dict[str, Any]:
    api = HfApi()
    folder = f"{date}"
    try:
        items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception:
        # Fallback: root listing then filter by date prefix
        items = api.list_repo_tree(repo=repo, path="", recursive=False)
        items = [it for it in items if it.path.startswith(f"{date}/")]

    files = [it.path for it in items if not it.path.endswith("/")]
    cdn_urls = [f"https://huggingface.co/datasets/{repo}/resolve/main/{p}" for p in files]
    return {"repo": repo, "date": date, "files": files, "cdn_urls": cdn_urls}

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset files for CDN-bypass ingestion")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/company-knowledge)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    result = list_hf_files(args.repo, args.date)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {len(result['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

## 3) Lightning Studio reuse/start guard (quota-safe)

`/opt/axentx/vanguard/discovery/ensure_studio.py`

```python
#!/usr/bin/env python3
"""
Reuse running Studio; restart if stopped; create only if absent.
Avoids blind create_ok=True quota burn.
"""
import argparse
import json
import sys
from typing import Dict, Any

try:
    from lightning import Studio, L40S, Teamspace
    _sdk_available = True
except Exception:  # noqa
    _sdk_available = False

if not _sdk_available:
    # Stub behavior for envs without lightning SDK (CI/local dev)
    class Studio:  # type: ignore
        @staticmethod
        def create_ok(*args, **kwargs):  # type: ignore
            return type("Studio", (), {"name": kwargs.get("name", "stub"), "id": "stub", "status": "created"})()
    class L40S:  # type: ignore
        pass
    class Teamspace:  # type: ignore
        studios: list = []

def ensure_studio(name: str, machine_type=L40S, timeout: int = 300) -> Dict[str, Any]:
    if not _sdk_available:
        return {"name": name, "status": "skipped_no_sdk", "id": None, "machine": machine_type.__name__}

    running = [s for s in Teamspace.studios if getattr(s, "name", None) == name and getattr(s, "status", None) == "running"]
    if running:
        studio = running[0]
        return {"name": name, "status": "reused", "id": getattr(studio, "id", None), "machine": machine_type.__name__}

    stopped = [s for s in Teamspace.studios if getattr(s, "name", None) == name and getattr(s, "status", None) == "stopped"]
    if stopped:
        studio = stopped[0]
        try:
            studio.start(machine=machine_type)
            return {"name": name, "status": "restarted", "id": getattr(st
