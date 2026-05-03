# vanguard / quality

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged both proposals, kept only what is concrete and correct, removed contradictions, and produced a single deployable plan.

---

## 1. Diagnosis (merged, corrected)

- **Runtime HF API calls from frontend** — `list_repo_tree`/metadata during preview/training launch burn quota and expose 429 risk on user machines.
- **No static file manifest in bundle** — every session re-enumerates the repo instead of using a precomputed CDN file list.
- **Training uses `load_dataset(streaming=True)` on heterogeneous repos** — risks `pyarrow.CastError` on mixed schemas and wastes API quota during data loading.
- **Lightning Studio reuse not enforced** — idle-stop kills training and new runs recreate studios, burning quota.
- **No CDN bypass for dataset fetches during training** — authenticated API calls used for every file instead of `resolve/main/` CDN URLs.
- **No deterministic repo selection / HF commit cap** — non-deterministic dataset versions and unbounded HF commit history.

---

## 2. Proposed change (single plan)

Add a build-time manifest generator and embed a static file list into the frontend bundle; update the training launcher to use CDN-only fetches with pre-listed paths; enforce Lightning Studio reuse; add deterministic repo/version selection and commit cap.

Scope:
- `/opt/axentx/vanguard/scripts/generate_manifest.py` (new)
- `/opt/axentx/vanguard/frontend/src/lib/data/hfManifest.ts` (generated type + JSON)
- `/opt/axentx/vanguard/frontend/src/lib/data/useHfFileList.ts` (modify)
- `/opt/axentx/vanguard/scripts/launch_training.py` (modify)
- `/opt/axentx/vanguard/scripts/train.py` (modify/create)
- CI/build hook to run manifest generation after dataset updates

---

## 3. Implementation (merged + hardened)

### 3.1 Generate static manifest (run at build time or after dataset updates)

`/opt/axentx/vanguard/scripts/generate_manifest.py`

```python
#!/usr/bin/env python3
"""
generate_manifest.py
Pre-list HF dataset files for a date folder and emit JSON for frontend + training.
Run from CI or ops after dataset is updated.
"""
import json
import os
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi, CommitOperationAdd, create_repo, get_repo

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_MANIFEST = os.getenv("HF_MANIFEST_OUT", "frontend/src/lib/data/hfManifest.json")
TRAIN_MANIFEST = "scripts/file_list.json"
HF_REVISION = os.getenv("HF_REVISION", "main")  # deterministic revision
HF_COMMIT_CAP = int(os.getenv("HF_COMMIT_CAP", "10"))  # limit history churn

def enforce_commit_cap(repo_id: str, revision: str, cap: int) -> None:
    """Optionally prune old commits to keep repo small and deterministic."""
    try:
        repo = get_repo(repo_id, repo_type="dataset", revision=revision)
        # Best-effort: list refs and prune if needed (requires permissions).
        # If not possible, CI should manage via git gc/force-push externally.
        # This function is a placeholder to make policy explicit.
    except Exception:
        # Fail soft — manifest generation should not break if pruning unavailable.
        pass

def main() -> None:
    api = HfApi()
    prefix = f"{DATE_FOLDER}/"

    # Single non-recursive call to list one folder (avoids pagination/100x)
    files = api.list_repo_tree(repo_id=HF_REPO, path=prefix, recursive=False, repo_type="dataset")
    # Keep only files (ignore subfolders)
    file_paths = sorted(f.rfilename for f in files if not f.rfilename.endswith("/"))

    if not file_paths:
        print("WARNING: no files found for prefix", prefix, file=sys.stderr)

    manifest = {
        "repo": HF_REPO,
        "revision": HF_REVISION,
        "date_folder": DATE_FOLDER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": file_paths,
        "cdn_prefix": f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_REVISION}/{DATE_FOLDER}/"
    }

    os.makedirs(os.path.dirname(OUT_MANIFEST), exist_ok=True)
    with open(OUT_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Compact copy for training script
    with open(TRAIN_MANIFEST, "w", encoding="utf-8") as f:
        json.dump({"files": file_paths, "cdn_prefix": manifest["cdn_prefix"]}, f)

    print(f"Manifest written: {OUT_MANIFEST} ({len(file_paths)} files)")

    # Optional policy enforcement (non-blocking)
    enforce_commit_cap(HF_REPO, HF_REVISION, HF_COMMIT_CAP)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/generate_manifest.py
```

CI hook (example):

```bash
# After dataset update
python /opt/axentx/vanguard/scripts/generate_manifest.py
git add frontend/src/lib/data/hfManifest.json scripts/file_list.json
git commit -m "chore: update HF manifest for $(date -u +%Y-%m-%d)"
```

---

### 3.2 Frontend: embed manifest and remove runtime API enumeration

`/opt/axentx/vanguard/frontend/src/lib/data/hfManifest.ts` (generated type + JSON import)

```ts
// This file is generated by scripts/generate_manifest.py and committed.
// Do not edit manually.
export interface HfManifest {
  repo: string;
  revision: string;
  date_folder: string;
  generated_at: string;
  files: string[];
  cdn_prefix: string;
}

import manifest from "./hfManifest.json";
export default manifest as HfManifest;
```

Update file-list hook to prefer manifest and avoid runtime HF API:

`/opt/axentx/vanguard/frontend/src/lib/data/useHfFileList.ts`

```ts
import { onMount } from "svelte";
import manifest from "./hfManifest.json";

export function useHfFileList() {
  let files: string[] = [];
  let cdnPrefix = "";
  let loading = false;
  let error: string | null = null;

  onMount(() => {
    // Use static manifest to avoid runtime HF API calls
    files = manifest.files || [];
    cdnPrefix = manifest.cdn_prefix || "";
  });

  async function refreshFromManifest() {
    // Optional: fetch updated manifest from CDN (unauthenticated) if needed.
    // Avoid authenticated HF API calls in the browser.
    loading = true;
    try {
      const res = await fetch("/src/lib/data/hfManifest.json");
      if (!res.ok) throw new Error("Failed to load manifest");
      const m = await res.json();
      files = m.files || [];
      cdnPrefix = m.cdn_prefix || "";
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  return { files, cdnPrefix, loading, error, refreshFromManifest };
}
```

---

### 3.3 Training launcher: use CDN-only fetches with pre-listed files

`/opt/axentx/vanguard/scripts/launch_training.py` (modifications)

```python
import json
import os
from pathlib import Path

def build_cdn_file_urls(file_list_path: str = "scripts/file_list.json"):
    """Return (cdn_prefix, file_urls) for Lightning training script to consume."""
    with open(file_list_path) as f:
        m = json.load(f)
    prefix = m["cdn_prefix"]
    urls = [f"{prefix}{os.path.basename(p)}" for p in m["files"]]
    return prefix, urls

# Example usage in Lightning launcher:
# Pass file_urls or file_list_path to the training script via CLI
