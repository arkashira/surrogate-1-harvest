# vanguard / discovery

## 1) Diagnosis

- No executable discovery entrypoint exists to surface high-value knowledge (top-hub docs, contextual insights) before planning work — violates established pattern `#knowledge-rag #graph #hub`.
- Missing CDN-bypass file-list strategy for HF datasets; any future training ingestion will immediately hit 429 rate limits without pre-listed manifest and CDN-only fetches.
- No canonical “run market analysis → query top hub” workflow to bootstrap context for vanguard discovery.
- No reusable script to list a single date-folder tree once and embed the file manifest for Lightning training (CDN-only path).
- No guardrails to prevent `load_dataset(streaming=True)` on heterogeneous repos (pyarrow CastError risk for surrogate-1).

## 2) Proposed change

Create `/opt/axentx/vanguard/bin/discover` (executable) that:
- Runs `granite-business-research.sh` (if present) or a lightweight market scan stub.
- Queries knowledge-rag for the top-hub (e.g., “MOC”) and prints the most-connected doc summary.
- Generates `manifests/hf_files_YYYYMMDD.json` for a given HF dataset repo + date folder via single `list_repo_tree` call (non-recursive) to enable CDN-bypass ingestion.
- Emits a small `training_stub.py` that uses the manifest for CDN-only fetches (zero API calls during data load).

Scope:
- New file: `bin/discover`
- New dir: `manifests/`, `templates/`
- New file: `templates/training_stub.py.j2` (Jinja2 or simple string template)

## 3) Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/{bin,manifests,templates}
```

`/opt/axentx/vanguard/bin/discover`
```bash
#!/usr/bin/env bash
# discover - surface high-value knowledge + prepare HF CDN-bypass manifest
# Usage: ./discover [--hf-repo <repo>] [--date-folder <path>] [--out-dir <dir>]
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST_DIR="${BASE_DIR}/manifests"
TEMPLATE_PATH="${BASE_DIR}/templates/training_stub.py.j2"

HF_REPO=""
DATE_FOLDER=""
OUT_DIR="${MANIFEST_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hf-repo) HF_REPO="$2"; shift 2 ;;
    --date-folder) DATE_FOLDER="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --help) echo "Usage: $0 [--hf-repo repo] [--date-folder path] [--out-dir dir]"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

mkdir -p "${OUT_DIR}"

# 1) Market analysis (best-effort)
if command -v granite-business-research.sh >/dev/null 2>&1; then
  echo "[discover] Running market analysis..."
  granite-business-research.sh || echo "[discover] Market analysis completed with warnings."
else
  echo "[discover] granite-business-research.sh not found — skipping (stub)."
fi

# 2) Knowledge-RAG top-hub insight (stub; replace with real CLI/API)
echo "[discover] Querying top-hub (MOC) via knowledge-rag..."
if command -v knowledge-rag >/dev/null 2>&1; then
  knowledge-rag query --top-hub MOC --limit 3 --format summary || echo "[discover] knowledge-rag unavailable — skipping."
else
  echo "[discover] knowledge-rag not found — skipping (stub). Recommended: review most-connected hub (MOC) manually."
fi

# 3) HF CDN-bypass manifest (single API call)
if [[ -n "${HF_REPO}" && -n "${DATE_FOLDER}" ]]; then
  echo "[discover] Generating HF file manifest for ${HF_REPO}/${DATE_FOLDER} ..."
  python3 - "$HF_REPO" "$DATE_FOLDER" "${OUT_DIR}" <<'PYEOF'
import json, os, sys
try:
    from huggingface_hub import HfApi
except ImportError:
    print("[discover] huggingface_hub not installed — skipping manifest generation.", file=sys.stderr)
    sys.exit(0)

repo = sys.argv[1]
folder = sys.argv[2].lstrip("/")
out_dir = sys.argv[3]

api = HfApi()
# Non-recursive list for the folder; caller can run per subfolder if needed
items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
files = []
for item in items:
    if item.rfilename:
        # CDN URL (no auth) for public datasets
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.rfilename}"
        files.append({
            "path": item.rfilename,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })
manifest_path = os.path.join(out_dir, f"hf_files_{os.path.basename(folder) or 'latest'}.json")
os.makedirs(out_dir, exist_ok=True)
with open(manifest_path, "w") as f:
    json.dump({"repo": repo, "folder": folder, "files": files}, f, indent=2)
print(f"[discover] Manifest written to {manifest_path} ({len(files)} files)")
PYEOF
else
  echo "[discover] Skipping HF manifest (provide --hf-repo and --date-folder)."
fi

# 4) Emit training stub using manifest
STUB_PATH="${OUT_DIR}/training_stub.py"
if [[ -f "${TEMPLATE_PATH}" ]]; then
  # If j2 available, render; else simple copy
  if command -v jinja2 >/dev/null 2>&1 || python3 -c "import jinja2" 2>/dev/null; then
    python3 -c "
import json, sys, os
from jinja2 import Template
with open('${TEMPLATE_PATH}') as f:
    t = Template(f.read())
manifest_files = []
mf = '${MANIFEST_DIR}'
if os.path.exists(mf):
    for x in sorted(os.listdir(mf)):
        if x.endswith('.json'):
            with open(os.path.join(mf, x)) as d:
                manifest_files.append(json.load(d))
out = t.render(manifests=manifest_files)
with open('${STUB_PATH}', 'w') as outf:
    outf.write(out)
"
  else
    # Fallback: simple stub
    cat > "${STUB_PATH}" <<'STUB'
# training_stub.py - CDN-only HF dataset loader (zero API calls during training)
# Usage: Set MANIFEST_PATH to a JSON produced by discover.
import json, os, sys, torch
from torch.utils.data import Dataset, DataLoader
from typing import List, Dict

HF_MANIFEST = os.getenv("HF_MANIFEST", "manifests/hf_files_latest.json")

class CDNTextDataset(Dataset):
    def __init__(self, manifest_path: str, max_files: int = -1):
        with open(manifest_path) as f:
            self.meta = json.load(f)
        self.files: List[Dict] = self.meta.get("files", [])
        if max_files > 0:
            self.files = self.files[:max_files]

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        info = self.files[idx]
        url = info["cdn_url"]
        # Lightweight streaming read (no auth). Replace with real parsing.
        import requests
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        text = resp.text
        # Project to {prompt, response} here per file if needed.
        return {"text": text, "path": info["path"], "cdn_url": url}

if __name__ == "__main__":
    ds = CDNTextDataset(HF_MANIFEST, max_files=10)
    print(f"Loaded {len(ds)} files from manifest {HF_MANIFEST}")
    for i in range(min(3, len(ds))):
        print(ds[i]["path"])
STUB
  fi
  echo "[discover] Training stub written to ${STUB_PATH}"
fi

echo "[discover] Done. Next steps:"
echo "  - Review top-hub insights above."
echo "  -
