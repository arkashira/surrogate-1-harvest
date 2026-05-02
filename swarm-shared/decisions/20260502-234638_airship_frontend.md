# airship / frontend

## Final Synthesis — Airship `/discover` (CDN-only manifest + 1-click training)

**Chosen improvement**  
Add a lightweight `/discover` page that shows the latest CDN-only manifest and a **“Copy CDN train script”** button.  
Goal: unblock immediate surrogate-1 training iterations with deterministic CDN-only file lists and zero HuggingFace API calls during data load.

---

### Key decisions (resolve contradictions)

- **Correctness + safety**: require explicit `repo_id` and `date_folder` in the URL (not optional at API layer) to avoid ambiguous “latest” behavior and path-traversal risks.  
- **Actionability**: provide a complete, copy-pasteable `train.py` that uses **CDN URLs only** and runs on Lightning Studio or local GPU.  
- **Performance**: backend can optionally cache the manifest in memory for ~5 min, but must never silently re-run expensive discovery without user intent.  
- **UX**: one-click copy for both manifest JSON and training script; monospace paths; human sizes; short hashes; clear errors and CLI fallback.

---

### Implementation plan (≤2h)

1. **Backend** — FastAPI endpoint `/api/discover/{repo_id}/{date_folder}` (15–20m)  
   - Validate path traversal.  
   - Read `manifests/{repo_id}/{date_folder}.json` (or generate via `list_repo_tree` if you prefer on-demand).  
   - Optional: short in-memory cache keyed by `(repo_id, date_folder)` to dedupe rapid calls.  
   - Return normalized JSON: `{ repo_id, date_folder, generated_at, count, files: [{ path, cdn_url, size, sha256 }] }`.

2. **Frontend** — `/discover` page (45–60m)  
   - Fetch manifest via URL params (`/discover/{repo_id}/{date_folder}`).  
   - Render table: #, Path (monospace), Size, SHA256 (short).  
   - Show metadata: file count, generated timestamp, Refresh hint.  
   - Buttons:  
     - **Copy CDN train script** → copies complete `train.py` using CDN URLs.  
     - **Copy manifest JSON** → copies normalized manifest.  
   - Responsive, small, clear error states and CLI fallback message.

3. **Polish + tests** (20–30m)  
   - Validate endpoint edge cases (missing manifest, malformed JSON).  
   - Ensure copy-to-clipboard works and script is valid Python.  
   - Buffer for integration and path adjustments.

---

### Backend (FastAPI snippet)

```python
# arkship/main.py (or api/discover.py)
from fastapi import APIRouter, HTTPException
from pathlib import Path
import json
import time
from typing import Dict, Any

router = APIRouter()
MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"

# Optional simple in-memory cache: (data, expires_at)
_CACHE: Dict[str, tuple[Any, float]] = {}
CACHE_TTL = 5 * 60  # 5 minutes

def _read_manifest(repo_id: str, date_folder: str):
    cache_key = f"{repo_id}/{date_folder}"
    now = time.time()
    if cache_key in _CACHE:
        data, expires = _CACHE[cache_key]
        if now < expires:
            return data

    manifest_path = (MANIFEST_ROOT / repo_id / f"{date_folder}.json").resolve()
    try:
        manifest_path.relative_to(MANIFEST_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id or date_folder")

    if not manifest_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "Manifest not found. Run: "
                f"airship discover --repo {repo_id} --date {date_folder}"
            )
        )

    try:
        raw = json.loads(manifest_path.read_text())
        files = raw.get("files", [])
        data = {
            "repo_id": repo_id,
            "date_folder": date_folder,
            "generated_at": raw.get("generated_at", manifest_path.stat().st_mtime),
            "count": len(files),
            "files": [
                {
                    "path": f.get("path"),
                    "cdn_url": f.get("cdn_url"),
                    "size": f.get("size"),
                    "sha256": f.get("sha256"),
                }
                for f in files
            ],
        }
        _CACHE[cache_key] = (data, now + CACHE_TTL)
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read manifest: {exc}")

@router.get("/discover/{repo_id}/{date_folder}")
async def get_manifest(repo_id: str, date_folder: str):
    return _read_manifest(repo_id, date_folder)
```

---

### Frontend (`/discover` page) — Vue 3 example

```vue
<template>
  <div class="discover">
    <h1>CDN Manifest — {{ repoId }} / {{ dateFolder }}</h1>

    <div v-if="loading">Loading manifest…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <div v-else-if="manifest" class="results">
      <div class="meta">
        <span>Files: {{ manifest.count }}</span>
        <span>Generated: {{ formatDate(manifest.generated_at) }}</span>
      </div>

      <div class="actions">
        <button @click="copyTrainScript" class="primary">Copy CDN train script</button>
        <button @click="copyManifest" class="secondary">Copy manifest JSON</button>
      </div>

      <table class="file-table">
        <thead>
          <tr><th>#</th><th>Path</th><th>Size</th><th>SHA256</th></tr>
        </thead>
        <tbody>
          <tr v-for="(f, i) in manifest.files" :key="i">
            <td>{{ i + 1 }}</td>
            <td class="path">{{ f.path }}</td>
            <td>{{ formatBytes(f.size) }}</td>
            <td class="hash">{{ shortHash(f.sha26) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
const repoId = route.params.repo_id || "";
const dateFolder = route.params.date_folder || "";

const manifest = ref(null);
const loading = ref(false);
const error = ref(null);

async function fetchManifest() {
  if (!repoId || !dateFolder) {
    error.value = "repo_id and date_folder are required.";
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    const res = await fetch(`/api/discover/${encodeURIComponent(repoId)}/${encodeURIComponent(dateFolder)}`);
    if (!res.ok) throw new Error(await res.text());
    manifest.value = await res.json();
  } catch (e) {
    error.value = e.message || "Failed to load manifest";
  } finally {
    loading.value = false;
  }
}

function formatBytes(n) {
  if (!n && n !== 0) return "-";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
  return (n / (1024 * 1024 * 1024)).toFixed(1) + " GB";
}

function formatDate(ts) {
  if (!ts) return "-";
  if (typeof ts === "number" && ts < 1e10) return new Date(ts * 1000).toISO
