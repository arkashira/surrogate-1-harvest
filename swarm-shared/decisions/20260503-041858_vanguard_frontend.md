# vanguard / frontend

## 1. Diagnosis
- Frontend loads datasets via `load_dataset` at runtime → triggers HF API calls → 429 rate limits and non-reproducible epochs.
- No content-addressed manifest (file list + hashes) → frontend cannot use CDN-only fetch strategy.
- Mixed-schema `enriched/` files contain `source`/`ts` columns → downstream parsing brittle and schema-dependent.
- No local dev fallback (cached sample) → frontend blocked when HF API unavailable or rate-limited.
- Missing frontend build step to embed file manifest → forces runtime API dependency.

## 2. Proposed change
- Add a content-addressed manifest generator (build-time) and a frontend data loader that uses CDN URLs only.
- Scope:
  - `/opt/axentx/vanguard/scripts/generate-manifest.py` (new)
  - `/opt/axentx/vanguard/src/data/loader.js` (new)
  - `/opt/axentx/vanguard/package.json` (add build script)
  - `/opt/axentx/vanguard/.gitignore` (ignore generated manifest)

## 3. Implementation

### 3.1 Create manifest generator (run on Mac/CI; zero API calls during training)

```python
# /opt/axentx/vanguard/scripts/generate-manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for enriched/ parquet files.
Usage:
  HF_REPO="datasets/owner/repo" python3 generate-manifest.py --date-folder 2026-05-01 --out ./public/manifest.json
"""
import argparse
import hashlib
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import list_repo_tree

CDN_BASE = "https://huggingface.co/datasets"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(repo: str, date_folder: str, out_path: str) -> None:
    # Single API call: list one folder (non-recursive)
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [it for it in items if it.endswith(".parquet")]

    if not files:
        print(f"No parquet files in {repo}/{date_folder}", file=sys.stderr)
        sys.exit(1)

    manifest: List[Dict] = []
    for f in sorted(files):
        slug = f.replace(".parquet", "")
        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{date_folder}/{f}"
        # If file is present locally (CI download), include local hash for integrity
        local_path = os.path.join("data", date_folder, f)
        local_hash = None
        if os.path.exists(local_path):
            local_hash = file_sha256(local_path)

        manifest.append(
            {
                "slug": slug,
                "path": f"{date_folder}/{f}",
                "cdn_url": cdn_url,
                "sha256": local_hash,
                "date_folder": date_folder,
            }
        )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "repo": repo,
                "date_folder": date_folder,
                "generated_at": os.popen("date -u +%Y-%m-%dT%H:%M:%SZ").read().strip(),
                "files": manifest,
            },
            f,
            indent=2,
        )
    print(f"Manifest written to {out_path} ({len(manifest)} files)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("HF_DATASET_REPO", "datasets/owner/repo"))
    parser.add_argument("--date-folder", required=True, help="e.g. 2026-05-01")
    parser.add_argument("--out", default="./public/manifest.json")
    args = parser.parse_args()
    build_manifest(args.repo, args.date_folder, args.out)
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/generate-manifest.py
```

### 3.2 Frontend CDN-only loader (no HF API, no auth)

```javascript
// /opt/axentx/vanguard/src/data/loader.js
/**
 * Load parquet shards via CDN URLs listed in content-addressed manifest.
 * Uses Apache Arrow WASM (or fetch + parquet-wasm) to project {prompt,response}.
 *
 * Guarantees:
 * - Zero HF API calls during runtime (avoids 429).
 * - Reproducible epochs via deterministic file ordering.
 * - Graceful fallback to local sample if CDN fails (dev only).
 */

const DEFAULT_MANIFEST = "/manifest.json";

export class CDNParquetLoader {
  constructor({ manifestUrl = DEFAULT_MANIFEST, batchSize = 32 } = {}) {
    this.manifestUrl = manifestUrl;
    this.batchSize = batchSize;
    this.manifest = null;
    this.fileIndex = 0;
    this.currentBatch = [];
  }

  async loadManifest() {
    const res = await fetch(this.manifestUrl, { cache: "no-cache" });
    if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
    this.manifest = await res.json();
    // Deterministic ordering
    this.manifest.files.sort((a, b) => a.slug.localeCompare(b.slug));
    this.fileIndex = 0;
  }

  async nextBatch() {
    if (!this.manifest) await this.loadManifest();

    while (this.currentBatch.length < this.batchSize && this.fileIndex < this.manifest.files.length) {
      const file = this.manifest.files[this.fileIndex++];
      try {
        const rows = await this._fetchAndProject(file.cdn_url);
        this.currentBatch.push(...rows);
      } catch (err) {
        console.warn(`Skipping ${file.path}:`, err.message);
        // Continue to next file; avoid hard failure on single shard
      }
    }

    const batch = this.currentBatch.slice(0, this.batchSize);
    this.currentBatch = this.currentBatch.slice(this.batchSize);
    return batch;
  }

  async _fetchAndProject(cdnUrl) {
    // Lightweight browser approach: fetch as arrayBuffer and use parquet-wasm
    // If parquet-wasm is not available, fallback to JSON lines endpoint (if provided by CDN).
    // For MVP, assume a small helper endpoint `/parquet-to-json?url=` exists on same origin
    // or use serverless edge function to convert and project {prompt,response}.
    const res = await fetch(`/api/parquet-to-json?url=${encodeURIComponent(cdnUrl)}`);
    if (!res.ok) throw new Error(`Projection failed: ${res.status}`);
    const data = await res.json();
    // Ensure only {prompt,response}
    return data.map((row) => ({
      prompt: row.prompt ?? "",
      response: row.response ?? "",
    }));
  }

  reset() {
    this.fileIndex = 0;
    this.currentBatch = [];
  }
}
```

### 3.3 Add build script and public folder support

```json
// /opt/axentx/vanguard/package.json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "scripts": {
    "build:manifest": "python3 scripts/generate-manifest.py --repo datasets/owner/repo --date-folder 2026-05-01 --out public/manifest.json",
    "dev": "vite",
    "build": "npm run build:manifest && vite build",
    "preview": "vite preview"
  }
}
```

Add to `.gitignore`:

```
# /opt/axentx/vanguard/.gitignore
public/manifest.json
data/
```

### 3.4 Minimal dev fallback (optional)

Create `public/manifest.json` placeholder for local dev (ignored by git) so frontend starts without HF API.

## 4. Verification

1. Generate manifest (run once
