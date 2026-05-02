# vanguard / frontend

## Final Synthesis (Best Parts + Correctness + Actionability)

### Diagnosis (merged, corrected)
- Frontend repeatedly calls `list_repo_tree` on mount/navigation instead of reading a persisted manifest → burns HF quota (1000 req/5 min) and causes 429s.
- No client-side cache (localStorage/indexedDB) → slow UI, redundant requests on navigation, and no resilience when offline/rate-limited.
- Dataset file list is dynamic per date folder but changes infrequently (daily cadence) → ideal for static pre-generation + CDN hosting + client cache with TTL.
- UI blocks on API during training config → poor perceived performance and quota waste.
- Missing fallback on 429 → hard UI errors instead of graceful degradation to cached/embedded manifest.

---

### Proposed Change (merged, prioritized)
1. **Pre-generate a static `file-manifest.json` per date folder** at build/deploy time (or via orchestration script) and host it on CDN (HF resolve URL).  
   - Single source of truth; avoids per-user API calls.
2. **Add a frontend manifest cache layer** with:
   - CDN-first fetch (no auth, bypasses API limits).
   - localStorage cache keyed by `vanguard:manifest:{repo}:{date}` with 24h TTL.
   - Graceful fallback to cache when CDN or API fails.
   - Optional API fallback only when explicitly allowed (and guarded against 429).
3. **Replace all `list_repo_tree` calls in UI components** with `getDatasetFiles(...)` from the new cache layer.
4. **Add optimistic UI** using cached manifest so training config/file pickers render instantly.

Scope:
- `/opt/axentx/vanguard/src/lib/hf-client.ts` — manifest loader + cache.
- `/opt/axentx/vanguard/src/components/` (file picker/dataset selector) — consume cached manifest.
- `/opt/axentx/vanguard/scripts/generate-manifest.js` — pre-generation script.

---

### Implementation (corrected, executable)

#### 1. Create directories
```bash
mkdir -p /opt/axentx/vanguard/src/lib
mkdir -p /opt/axentx/vanguard/scripts
```

#### 2. Manifest generator (CDN-ready)
`/opt/axentx/vanguard/scripts/generate-manifest.js`
```js
#!/usr/bin/env node
/**
 * Generate a file manifest for a date folder.
 * Usage: node generate-manifest.js <owner>/<dataset> <date-folder> [output.json]
 * Requires HF_TOKEN for one-time list_repo_tree call.
 * Emits JSON to be committed or uploaded to CDN.
 */

import { HfApi } from "@huggingface/hub";
import fs from "fs";

async function main() {
  const repo = process.argv[2];
  const dateFolder = process.argv[3];
  const outPath = process.argv[4] || "file-manifest.json";

  if (!repo || !dateFolder) {
    console.error("Usage: node generate-manifest.js <repo> <date-folder> [out.json]");
    process.exit(1);
  }

  const api = new HfApi({ token: process.env.HF_TOKEN || undefined });

  // Single recursive call to capture full tree under dateFolder
  const tree = await api.listRepoTree({
    repo,
    path: dateFolder,
    recursive: true,
  });

  const files = tree
    .filter((entry) => entry.type === "file")
    .map((entry) => ({
      path: `${dateFolder}/${entry.path}`,
      size: entry.size || 0,
    }));

  const manifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };

  fs.writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Wrote ${files.length} files to ${outPath}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```
Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/generate-manifest.js
```

#### 3. Frontend HF client with cache (corrected + complete)
`/opt/axentx/vanguard/src/lib/hf-client.ts`
```ts
/**
 * HF client utilities for vanguard frontend.
 * Strategy:
 * - Prefer CDN manifest (no auth, bypasses API limits).
 * - Fallback to localStorage cache (TTL 24h).
 * - Optional API fallback only when explicitly allowed (guarded).
 */

const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h

function cacheKey(repo: string, dateFolder: string): string {
  return `vanguard:manifest:${repo}:${dateFolder}`;
}

export interface FileEntry {
  path: string;
  size: number;
}

export interface Manifest {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  files: FileEntry[];
}

function isManifest(obj: any): obj is Manifest {
  return (
    obj &&
    typeof obj.repo === "string" &&
    typeof obj.dateFolder === "string" &&
    typeof obj.generatedAt === "string" &&
    Array.isArray(obj.files) &&
    obj.files.every((f: any) => typeof f.path === "string" && typeof f.size === "number")
  );
}

export async function getCachedManifest(repo: string, dateFolder: string): Promise<Manifest | null> {
  try {
    const raw = localStorage.getItem(cacheKey(repo, dateFolder));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!isManifest(parsed)) return null;
    const generatedAt = new Date(parsed.generatedAt).getTime();
    if (Date.now() - generatedAt > CACHE_TTL_MS) {
      localStorage.removeItem(cacheKey(repo, dateFolder));
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export async function setCachedManifest(manifest: Manifest): Promise<void> {
  try {
    localStorage.setItem(cacheKey(manifest.repo, manifest.dateFolder), JSON.stringify(manifest));
  } catch {
    // ignore storage errors (private mode, quota)
  }
}

/**
 * Fetch manifest from CDN (no Authorization header) — bypasses HF API rate limits.
 * Example URL: https://huggingface.co/datasets/axentx/surrogate-1/resolve/main/2026-05-02/file-manifest.json
 */
export async function fetchManifestFromCDN(repo: string, dateFolder: string): Promise<Manifest | null> {
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${dateFolder}/file-manifest.json`;
  try {
    const res = await fetch(url, { cache: "no-cache" });
    if (!res.ok) return null;
    const parsed = await res.json();
    if (!isManifest(parsed)) return null;
    await setCachedManifest(parsed);
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Primary entrypoint for UI components: returns file list for a date folder.
 * Prefer CDN → localStorage cache → (optional) API fallback.
 */
export async function getDatasetFiles(
  repo: string,
  dateFolder: string,
  { allowApiFallback = false }: { allowApiFallback?: boolean } = {}
): Promise<FileEntry[]> {
  // 1) CDN manifest (fast, no auth, bypasses API limits)
  const cdnManifest = await fetchManifestFromCDN(repo, dateFolder);
  if (cdnManifest) return cdnManifest.files;

  // 2) localStorage cache
  const cached = await getCachedManifest(repo, dateFolder);
  if (cached) return cached.files;

  // 3) Optional API fallback (use sparingly; can trigger 429)
  if (allowApiFallback) {
    try {
      const { HfApi } = await import("@huggingface/hub");
      const api = new HfApi({ token: undefined });
      const tree = await api.listRepoTree({ repo, path: dateFolder, recursive: true });
      const files = tree
        .filter((e) => e.type === "file")
        .map((e) => ({ path: `${dateFolder}/${
