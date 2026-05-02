# vanguard / discovery

## Final Synthesized Implementation

**Core decision**: Merge the strongest elements from both candidates into a single, correct, and immediately actionable script.  
- Adopt Candidate 1’s concrete file paths, CDN-bypass training stub, and Lightning Studio reuse guard.  
- Adopt Candidate 2’s explicit CLI/wrapper hygiene (shebang, permissions, cron-readiness) and stronger discovery-surface framing.  
- Resolve contradictions in favor of correctness + actionability: always emit valid manifests, never auto-create studios in CI, and provide a deterministic fallback when `knowledge-rag` or HF API is unavailable.

---

### 1) Implementation

Create `/opt/axentx/vanguard/scripts/discover_and_plan.sh`:

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/scripts/discover_and_plan.sh
# Purpose: discovery anchor for vanguard — combines market research, knowledge-rag top-hub,
#          HF CDN manifest, and Lightning Studio reuse plan.
# Cron-ready: ensure executable and use absolute paths.
set -euo pipefail
SHELL=/bin/bash

PROJECT_ROOT="/opt/axentx/vanguard"
SCRIPTS_DIR="${PROJECT_ROOT}/scripts"
MANIFEST_DIR="${PROJECT_ROOT}/manifests"
TRAIN_STUB_DIR="${PROJECT_ROOT}/training"

mkdir -p "${MANIFEST_DIR}" "${TRAIN_STUB_DIR}"

# ---- 1) Optional market/competitor research ----
if [[ -x "${SCRIPTS_DIR}/granite-business-research.sh" ]]; then
  echo "== Running market research =="
  "${SCRIPTS_DIR}/granite-business-research.sh"
else
  echo "== No granite-business-research.sh found — skipping =="
fi

# ---- 2) Knowledge-rag top-hub insight ----
echo "== Querying knowledge-rag for top hub =="
TOP_HUB_FILE="${MANIFEST_DIR}/top_hub.json"
if command -v knowledge-rag &>/dev/null; then
  knowledge-rag top-hub --format json > "${TOP_HUB_FILE}" || true
else
  echo "knowledge-rag CLI not available; using deterministic fallback."
  # Deterministic fallback: prefer MOC as highest-value anchor
  echo '{"hub": "MOC", "reason": "fallback: maximize connectedness", "connections": 0}' > "${TOP_HUB_FILE}"
fi
# Always ensure non-empty, valid JSON
if [[ ! -s "${TOP_HUB_FILE}" ]] || ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${TOP_HUB_FILE}" 2>/dev/null; then
  echo '{"hub": "MOC", "reason": "fallback: maximize connectedness", "connections": 0}' > "${TOP_HUB_FILE}"
fi
cat "${TOP_HUB_FILE}"

# ---- 3) HF file manifest (CDN-bypass pattern) ----
HF_DATASET="${HF_DATASET:-datasets/your-org/surrogate-1}"
HF_DATE_FOLDER="${HF_DATE_FOLDER:-$(date +%Y-%m-%d)}"
MANIFEST_OUT="${MANIFEST_DIR}/file_list.json"

echo "== Building HF file manifest for ${HF_DATASET} :: ${HF_DATE_FOLDER} =="
python3 - <<PY > "${MANIFEST_OUT}"
import json, os, sys

MANIFEST = {
    "dataset_repo": "",
    "date_folder": "",
    "files": [],
    "note": "Use CDN URLs: https://huggingface.co/datasets/{dataset_repo}/resolve/main/{file}"
}

try:
    from huggingface_hub import HfApi
    api = HfApi()
    dataset_repo = os.getenv("HF_DATASET", "datasets/your-org/surrogate-1").replace("datasets/", "")
    date_folder = os.getenv("HF_DATE_FOLDER", "")
    tree = api.list_repo_tree(repo_id=dataset_repo, path=date_folder, recursive=False)
    files = [f.rfilename for f in tree if not f.rfilename.endswith("/") and getattr(f, "size", 0) > 0]
    MANIFEST["dataset_repo"] = dataset_repo
    MANIFEST["date_folder"] = date_folder
    MANIFEST["files"] = sorted(files)
except Exception as e:
    # Deterministic, safe fallback: empty manifest prevents 429/schema errors
    sys.stderr.write(f"HF manifest failed ({e}); using empty safe manifest.\n")
    MANIFEST["dataset_repo"] = os.getenv("HF_DATASET", "datasets/your-org/surrogate-1").replace("datasets/", "")
    MANIFEST["date_folder"] = os.getenv("HF_DATE_FOLDER", "")

json.dump(MANIFEST, sys.stdout, indent=2)
PY

echo "Manifest written to ${MANIFEST_OUT}"

# ---- 4) Training stub (CDN-only) ----
TRAIN_STUB="${TRAIN_STUB_DIR}/train_cdn_only.py"
cat > "${TRAIN_STUB}" <<'PY'
import json, os, sys
from pathlib import Path
import requests
from tqdm import tqdm

MANIFEST = os.getenv("MANIFEST_PATH", "manifests/file_list.json")
MAX_FILES = int(os.getenv("MAX_FILES", "10"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "50"))

class CDNParquetIterable:
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            m = json.load(f)
        self.files = m.get("files", [])
        self.repo = m.get("dataset_repo", "")
        if max_files:
            self.files = self.files[:max_files]

    def _stream_file(self, fn):
        url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{fn}"
        # Stream without HF API auth; project to {prompt,response} in real usage
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        # Placeholder: yield one record per file; replace with pyarrow parsing
        yield {"prompt": f"file://{fn}", "response": "placeholder"}

    def __iter__(self):
        for fn in self.files:
            yield from self._stream_file(fn)

def main():
    ds = CDNParquetIterable(
        manifest_path=os.getenv("MANIFEST_PATH", "manifests/file_list.json"),
        max_files=MAX_FILES
    )
    for i, batch in enumerate(tqdm(ds, desc="CDN stream", unit="file")):
        if i >= MAX_STEPS:
            break
        # Replace with real training step
        pass
    print("CDN-only data loop complete (no HF API calls).")

if __name__ == "__main__":
    main()
PY

echo "Training stub written to ${TRAIN_STUB}"

# ---- 5) Lightning Studio reuse plan (no auto-create in CI) ----
cat > "${TRAIN_STUB_DIR}/lightning_plan.md" <<'MD'
Lightning Studio reuse checklist (do not auto-create in CI):
- List Teamspace.studios and reuse any running studio with matching name.
- Prefer Machine.L40S on lightning-public-prod for free-tier; H200 requires lightning-lambda-prod (paid).
- Check studio status before .run(); if stopped, restart with target.start(machine=...).
- Set idle timeout handling: checkpoint frequently; avoid relying on long-running sessions.

Quick launcher (Python snippet):
```python
from lightning_sdk import Studio, Teamspace, Machine

TARGET = "vanguard-train"
studio = None
for s in Teamspace.studios:
    if s.name == TARGET and s.status == "running":
        studio = s
        break

if studio is None:
    # Do NOT auto-create in CI; uncomment only for interactive use
    # studio = Studio.create(name=TARGET, machine=Machine.L40S, cluster="lightning-public-prod")
    print(f"No running studio named {TARGET}. Create manually if needed.")
else:
    print(f"Reusing running studio: {studio.name}")
```
MD

echo "== Done. Next steps =="
echo "1) Review manifests/top_hub.json for discovery insights."
echo "2) Test CDN-only stub: MANIFEST_PATH=manifests/file_list.json python3 training/train_cdn_only.py"
echo "3) Use lightning_plan.md to start/reuse a Lightning Studio (do not auto-create in CI)."
```

Make executable (cron-ready):

```bash
chmod +x /opt/axentx/vanguard/scripts/discover_and_plan.sh
```

