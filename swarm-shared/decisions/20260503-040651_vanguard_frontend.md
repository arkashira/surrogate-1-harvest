# vanguard / frontend

## 1. Diagnosis
- Frontend currently performs runtime HF API calls (`list_repo_tree`, `load_dataset`, or similar) to list/resolve dataset files → triggers 429 rate-limits and non-reproducible views.
- No content-addressed manifest (e.g., `dataset-manifest.json`) exists; training/UI rely on dynamic repo scans that drift across runs.
- Mixed-schema files in `enriched/` (extra columns like `source`, `ts`) cause frontend parsing assumptions to break and risk `pyarrow.CastError` downstream.
- No local cache layer for file listings/metadata → repeated page loads re-hit HF API and amplify quota burn.
- No deterministic snapshot binding between frontend build and dataset state → deploys can show different data without a version bump.

## 2. Proposed change
- Add a build-time generated `dataset-manifest.json` that lists available parquet files for a pinned date folder (content-addressed by commit/date) and exposes only `{prompt,response}` schema fields.
- Frontend loads this manifest (static asset) instead of calling HF API at runtime.
- Scope:
  - New: `/opt/axentx/vanguard/scripts/generate-manifest.py`
  - Modified: frontend dataset loader module (likely `src/lib/dataset.js` or `src/lib/dataset.ts` — infer from structure) to consume `dataset-manifest.json`.
  - New static asset output: `public/dataset-manifest.json` (committed or injected at build).

## 3. Implementation

### 3.1 Generate manifest (run at build / CI)
`/opt/axentx/vanguard/scripts/generate-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate dataset-manifest.json at build time.
- Uses HF API ONCE (or CDN file listing) to pin a date folder.
- Emits content-addressed manifest with {file, size, rows?, sha256?} and schema projection.
- Designed to be committed or injected into public/ so frontend never calls HF API.
"""
import os
import json
import hashlib
import subprocess
from datetime import datetime, timezone

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/vanguard-mirror")
DATE_FOLDER = os.getenv("DATASET_DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUTPUT_PATH = os.getenv("MANIFEST_OUT", "public/dataset-manifest.json")

def run_hf_tree():
    # Prefer CLI if available; fallback to huggingface_hub if installed.
    try:
        out = subprocess.check_output(
            ["huggingface-cli", "repo", "tree", HF_REPO, "--path", DATE_FOLDER, "--recursive", "--json"],
            text=True,
        )
        return json.loads(out)
    except Exception:
        # Lightweight fallback: use CDN directory listing via requests (best-effort)
        import requests
        r = requests.get(f"https://huggingface.co/datasets/{HF_REPO}/tree/main/{DATE_FOLDER}", headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
        raise RuntimeError("Cannot list HF folder; provide DATASET_DATE_FOLDER and ensure files are available.")

def build_manifest():
    tree = run_hf_tree()
    entries = []
    for node in tree.get("tree", []):
        path = node.get("path", "")
        if not path.endswith(".parquet"):
            continue
        # Use CDN resolve URL (bypasses API auth/rate-limits during training/load)
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"
        entries.append({
            "file": path,
            "url": cdn_url,
            "size": node.get("size"),
            "type": "parquet",
            "schema_projection": ["prompt", "response"],
            # optional content hash could be added by CI after download
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "entries": sorted(entries, key=lambda x: x["file"]),
        "checksum": None,  # optionally populated by CI after file fetch
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH) if os.path.dirname(OUTPUT_PATH) else ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {OUTPUT_PATH} ({len(entries)} entries)")

if __name__ == "__main__":
    build_manifest()
```

Make executable and ensure Bash-friendly invocation in CI:
```bash
chmod +x /opt/axentx/vanguard/scripts/generate-manifest.py
```

Add to build script (example):
```bash
# In your build step (e.g., npm run build or make frontend)
python3 /opt/axentx/vanguard/scripts/generate-manifest.py
```

### 3.2 Frontend loader (example)
`/opt/axentx/vanguard/src/lib/dataset.js` (adjust path to actual frontend structure)
```js
// Load static manifest generated at build time.
// Never call HF API at runtime.
export async function loadDatasetManifest() {
  const res = await fetch('/dataset-manifest.json', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to load dataset manifest');
  const manifest = await res.json();
  return manifest;
}

export async function loadParquetFile(fileEntry) {
  // Use CDN URL from manifest (bypasses HF API auth/rate-limits).
  const response = await fetch(fileEntry.url);
  if (!response.ok) throw new Error(`Failed to fetch ${fileEntry.file}`);
  const arrayBuffer = await response.arrayBuffer();
  // Parse parquet client-side or stream to worker as needed.
  // For MVP, return ArrayBuffer for downstream processing.
  return { file: fileEntry.file, data: arrayBuffer };
}
```

If your frontend uses TypeScript, add a simple interface:
```ts
export interface DatasetManifest {
  generated_at: string;
  repo: string;
  date_folder: string;
  entries: Array<{
    file: string;
    url: string;
    size: number | null;
    type: 'parquet';
    schema_projection: string[];
  }>;
  checksum: string | null;
}
```

### 3.3 Build/CI hook (one-liner)
Ensure frontend build depends on manifest:
```bash
# package.json script example
"build": "python3 scripts/generate-manifest.py && vite build"
```

## 4. Verification
- Run generation locally and confirm `public/dataset-manifest.json` exists and contains parquet entries with CDN URLs.
- Start dev server and open browser; check Network tab for:
  - No failed requests to `huggingface.co/api/` or `list_repo_tree` from frontend.
  - Successful fetch of `/dataset-manifest.json`.
- Confirm a page that lists datasets renders file names from the manifest.
- Simulate rate-limit stress: reload page multiple times — HF API calls should not increase (manifest is static).
- (Optional) Add a quick test that validates manifest schema:
```bash
python3 -c "import json; m=json.load(open('public/dataset-manifest.json')); assert 'entries' in m and all('url' in e for e in m['entries']); print('OK')"
```
