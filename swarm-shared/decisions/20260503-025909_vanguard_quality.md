# vanguard / quality

## Final Synthesis (Best Parts + Resolved Contradictions)

Below is the single, authoritative plan that merges the strongest, most actionable parts of both proposals and resolves contradictions in favor of **correctness** and **concrete actionability**.

---

## 1. Diagnosis (Consolidated)
- **No build-time deterministic asset manifest** → frontend and training rely on runtime HF API calls (invites 429s, breaks CDN-first strategy).
- **No mount-point / entrypoint boundary** between orchestration host (Mac) and compute target (Lightning) → fragile paths, no clear contract for file lists, machine spec, or Studio reuse.
- **Training ingestion uses `load_dataset(streaming=True)` on heterogeneous repos** → `pyarrow.CastError` on mixed-schema files.
- **No content-hashed asset references** → cache churn and non-deterministic deployments.
- **No integrity verification for CDN downloads** → silent corruption risk during long training runs.
- **Lightning Studio lifecycle not managed** → idle stop kills training; quota wasted by recreation instead of reuse.

---

## 2. Proposed Change (Concrete, Actionable)
Create a **build-time asset pipeline** + **Lightning-aware orchestration contract**:

1. **Deterministic manifest generator** (single CLI, one HF API call per date folder) → emits `assets-manifest.json` with `{slug: {sha256, size, cdnUrl, sourcePath, dateFolder}}`.
2. **Frontend imports static manifest** (bundler import or one-time fetch) → zero runtime HF API calls.
3. **Training reads same manifest and fetches via CDN** with SHA256 integrity checks → zero HF API calls during data load.
4. **Per-file projection layer** to normalize heterogeneous files to `{prompt, response}` before training (avoids `pyarrow.CastError`).
5. **Explicit mount-point / entrypoint contract**:
   - Host (Mac) produces `assets-manifest.json` + `lightning_job.yaml`.
   - Lightning target consumes via `/opt/axentx/vanguard/mnt/{manifest,parquets,cfg}`.
   - Job spec declares machine type, idle timeout, and Studio reuse policy.
6. **Lightning Studio lifecycle hook** to prevent idle stop and enable reuse (start/attach/resume via CLI + Studio API).

---

## 3. Implementation (Single, Unified)

### 3.1 Build: Manifest Generator
`/opt/axentx/vanguard/build/mk-asset-manifest.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic asset manifest for a date folder.
Usage:
  HF_REPO=datasets/your/repo python mk-asset-manifest.py 2026-05-03 > src/frontend/assets-manifest.json
"""
import os
import sys
import json
import hashlib
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("Install: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

HF_REPO = os.getenv("HF_REPO")
if not HF_REPO:
    print("HF_REPO env var required (e.g. datasets/your/repo)", file=sys.stderr)
    sys.exit(1)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(date_folder: str):
    # One API call: non-recursive tree for the date folder
    tree = list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    entries = {}
    for item in tree:
        if item.type != "file":
            continue
        slug = item.path.replace("/", "-")
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{item.path}"
        # Download once to local temp for hashing (only during manifest generation)
        local_path = hf_hub_download(repo_id=HF_REPO, filename=item.path, repo_type="dataset")
        size = item.size or Path(local_path).stat().st_size
        digest = sha256_file(Path(local_path))
        entries[slug] = {
            "sha256": digest,
            "size": size,
            "cdnUrl": cdn_url,
            "sourcePath": item.path,
            "dateFolder": date_folder
        }
    manifest = {
        "repo": HF_REPO,
        "dateFolder": date_folder,
        "generatedBy": "mk-asset-manifest",
        "assets": entries
    }
    return manifest

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: mk-asset-manifest.py <date-folder>", file=sys.stderr)
        sys.exit(1)
    mf = build_manifest(sys.argv[1])
    json.dump(mf, sys.stdout, indent=2)
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/build/mk-asset-manifest.py
```

---

### 3.2 Frontend: CDN Loader (TypeScript)
`/opt/axentx/vanguard/src/frontend/cdn_loader.ts`

```ts
// Deterministic CDN loader using build-time manifest.
// Import the generated JSON at build time (bundler) or fetch once (runtime).

export interface AssetEntry {
  sha256: string;
  size: number;
  cdnUrl: string;
  sourcePath: string;
  dateFolder: string;
}

export interface Manifest {
  repo: string;
  dateFolder: string;
  generatedBy: string;
  assets: Record<string, AssetEntry>;
}

export function resolveCdnUrl(slug: string, manifest: Manifest): string {
  const entry = manifest.assets?.[slug];
  if (!entry) throw new Error(`Asset not found in manifest: ${slug}`);
  return entry.cdnUrl;
}

export async function fetchWithIntegrity(
  slug: string,
  manifest: Manifest,
  options: RequestInit = {}
): Promise<ArrayBuffer> {
  const entry = manifest.assets?.[slug];
  if (!entry) throw new Error(`Asset not found in manifest: ${slug}`);
  const res = await fetch(entry.cdnUrl, { cache: "force-cache", ...options });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  const buf = await res.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", buf);
  const hex = Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  if (hex !== entry.sha256) throw new Error(`Integrity mismatch for ${slug}`);
  return buf;
}
```

---

### 3.3 Training: CDN Dataset + Projection
`/opt/axentx/vanguard/src/train/cdn_dataset.py`

```python
import json
import hashlib
import requests
from pathlib import Path
from typing import List, Dict

def load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path) as f:
        return json.load(f)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def cdn_fetch(entry: dict, dst_path: Path) -> Path:
    if dst_path.exists():
        if sha256_file(dst_path) == entry["sha256"]:
            return dst_path
        dst_path.unlink()
    resp = requests.get(entry["cdnUrl"], stream=True, timeout=60)
    resp.raise_for_status()
    with open(dst_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    if sha256_file(dst_path) != entry["sha256"]:
        raise RuntimeError(f"Integrity mismatch after download: {dst_path}")
    return dst_path

def project_jsonl_to_pair(local_path: Path) -> List[Dict[str, str]]:
    import json
