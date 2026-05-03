# vanguard / frontend

## Final Synthesized Solution

**Scope**:  
- `frontend/src/lib/api/file-manifest.ts` (new)  
- `frontend/src/lib/api/data.ts` (refactor)  
- Optional backend route: `/api/manifest/:repo/:dateFolder` (serves pre-generated or on-demand manifests)  
- Optional build-time script to pre-generate manifests for known `(repo, dateFolder)` pairs.

---

### 1. Diagnosis (consensus)

- Repeated authenticated `list_repo_tree` on page load burns HF quota (1000/5min) and causes 429s.  
- Using authenticated `/api/` proxy for dataset file downloads adds auth overhead and stricter limits vs public CDN.  
- No persisted `(repo, dateFolder)` manifest forces repeated expensive discovery.  
- No request deduplication/coalescing multiplies identical calls during rapid UI interactions.  
- Missing graceful fallback when HF API is unavailable causes UI crashes instead of degradation.

---

### 2. Core changes (combined + corrected)

1. **Replace repeated `list_repo_tree` with a manifest layer**  
   - One source of truth per `(repo, dateFolder)` (cached in `localStorage` + optional backend).  
   - TTL: 1 hour (correct balance between freshness and quota safety).  
   - Request deduplication: in-flight promises are coalesced so concurrent calls for same key share one network request.

2. **Use public CDN URLs for file downloads**  
   - `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth required).  
   - Remove authenticated proxy downloads for dataset files.

3. **Graceful degradation**  
   - If manifest fetch fails, return empty file list (UI shows empty state, not crash).  
   - If CDN fetch fails, surface actionable error to user (retry, report).

4. **Optional build-time pre-generation**  
   - Mac-side script to generate manifests for known folders to avoid runtime HF API calls entirely.  
   - Backend serves static manifests from `./manifests/{repo}/{dateFolder}.json`.

---

### 3. Implementation (final)

```ts
// frontend/src/lib/api/file-manifest.ts
const CDN_ROOT = 'https://huggingface.co/datasets';
const CACHE_TTL_MS = 1000 * 60 * 60; // 1h

interface FileManifest {
  repo: string;
  dateFolder: string;
  files: string[];
  generatedAt: number;
}

type Pending = Promise<string[]> | undefined;
const pending = new Map<string, Pending>();

function cacheKey(repo: string, dateFolder: string): string {
  return `hf-manifest:${repo}:${dateFolder}`;
}

function isFresh(manifest: FileManifest): boolean {
  return Date.now() - manifest.generatedAt < CACHE_TTL_MS;
}

async function fetchManifestFromBackend(
  repo: string,
  dateFolder: string
): Promise<FileManifest | null> {
  const res = await fetch(
    `/api/manifest/${encodeURIComponent(repo)}/${encodeURIComponent(dateFolder)}`,
    { credentials: 'same-origin' }
  );
  if (!res.ok) return null;
  return res.json();
}

export async function getFileList(
  repo: string,
  dateFolder: string
): Promise<string[]> {
  const key = cacheKey(repo, dateFolder);

  // 1) Try localStorage cache
  try {
    const cached = localStorage.getItem(key);
    if (cached) {
      const manifest: FileManifest = JSON.parse(cached);
      if (
        manifest.repo === repo &&
        manifest.dateFolder === dateFolder &&
        isFresh(manifest)
      ) {
        return manifest.files;
      }
    }
  } catch {
    try {
      localStorage.removeItem(key);
    } catch {}
  }

  // 2) Deduplicate in-flight requests
  const existing = pending.get(key);
  if (existing) return existing;

  const promise = (async () => {
    try {
      // Try backend manifest first (pre-generated or generated on-demand)
      const remote = await fetchManifestFromBackend(repo, dateFolder);
      if (remote && remote.repo === repo && remote.dateFolder === dateFolder) {
        localStorage.setItem(key, JSON.stringify(remote));
        return remote.files;
      }
    } catch {
      // fall through
    }

    // Graceful fallback: return empty list so UI doesn't crash
    console.warn('Could not fetch file manifest for', repo, dateFolder);
    return [];
  })();

  pending.set(key, promise);

  try {
    const files = await promise;
    return files;
  } finally {
    pending.delete(key);
  }
}

export function getCdnDownloadUrl(repo: string, filePath: string): string {
  // filePath is relative to dataset root (e.g., "2026-04-29/batch-123.parquet")
  return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}
```

```ts
// frontend/src/lib/api/data.ts
import { getFileList, getCdnDownloadUrl } from './file-manifest';

export async function listAvailableFiles(
  repo: string,
  dateFolder: string
): Promise<string[]> {
  return getFileList(repo, dateFolder);
}

export async function downloadFileAsBlob(
  repo: string,
  filePath: string
): Promise<Blob> {
  const url = getCdnDownloadUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
  }
  return res.blob();
}
```

Optional backend route (Node/Express-style):

```ts
// backend/routes/manifest.ts
import express from 'express';
import fs from 'fs';
import path from 'path';

const router = express.Router();
const MANIFEST_DIR = path.join(process.cwd(), 'manifests');

router.get('/manifest/:repo/:dateFolder', (req, res) => {
  const { repo, dateFolder } = req.params;
  const safeRepo = repo.replace(/[^a-zA-Z0-9\-_\.]/g, '');
  const safeDate = dateFolder.replace(/[^0-9\-]/g, '');
  const file = path.join(MANIFEST_DIR, safeRepo, `${safeDate}.json`);

  if (!fs.existsSync(file)) {
    return res.status(404).json({ error: 'manifest not found' });
  }

  const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
  return res.json(manifest);
});

export default router;
```

Mac-side generation script (run once per new folder):

```bash
#!/usr/bin/env bash
# scripts/generate-manifest.sh
# Usage: HF_TOKEN=... ./scripts/generate-manifest.sh myorg/surrogate-1 2026-04-29

set -euo pipefail
REPO="${1:?repo required}"
DATEFOLDER="${2:?dateFolder required}"
OUTDIR="manifests/${REPO}"
OUTFILE="${OUTDIR}/${DATEFOLDER}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json
from huggingface_hub import list_repo_tree

repo = os.environ["REPO"]
date = os.environ["DATEFOLDER"]
items = list_repo_tree(repo, path=date, recursive=False)
files = [f.rfilename for f in items if f.type == "file"]

manifest = {
    "repo": repo,
    "dateFolder": date,
    "files": sorted(files),
    "generatedAt": int(__import__("time").time() * 1000)
}

out = os.environ["OUTFILE"]
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(manifest, f)
print(f"Wrote {len(files)} files to {out}")
PY
```

---

### 4. Verification (concise checklist)

1. Generate manifest locally and confirm `manifests/{repo}/{dateFolder}.json` exists.  
2. Start app; open a dataset page.  
   - Network: no authenticated `list_repo_tree` calls.  
   - `/api/manifest/
