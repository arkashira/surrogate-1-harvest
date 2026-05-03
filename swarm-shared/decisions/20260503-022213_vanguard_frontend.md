# vanguard / frontend

## Final Synthesized Solution

### 1. Diagnosis (merged, de-duplicated)
- **No frontend manifest cache** for `(repo, dateFolder) → file-list`: every preview/training launch triggers authenticated HF API calls, burning quota and risking 429s.
- **No CDN-bypass path**: frontend fetches via authenticated endpoints instead of public CDN URLs (`/resolve/main/`), wasting rate-limit budget.
- **No persisted manifest artifact**: no `(repo, dateFolder) → file-list` JSON to embed in training scripts or frontend, forcing repeated discovery.
- **No schema projection preview**: UI shows raw heterogeneous files instead of projected `{prompt, response}` pairs, confusing reviewers and causing CastErrors.
- **No Lightning Studio reuse UI**: frontend creates new Studio instances instead of listing and reusing running ones, wasting quota.
- **Dataset preview likely uses `load_dataset(streaming=True)` or per-file authenticated fetches**, causing slow loads and schema errors on heterogeneous files.

### 2. Proposed Change (merged + concrete)
Add a frontend file-list cache + CDN loader + Studio reuse module in `/opt/axentx/vanguard/src/frontend/` (create if missing) with:

- `lib/fileCache.ts` — persist/load `(repo, dateFolder) → file-list` JSON to `localStorage` + TTL (24h) with LRU fallback.
- `lib/cdnLoader.ts` — build public CDN URLs and fetch via `fetch` (no auth) for previews; single `list_repo_tree` call from UI to populate cache (only after TTL expiry or manual refresh).
- `components/DataPreview.tsx` — render projected `{prompt, response}` pairs from cached file list and CDN samples; handle parquet via WASM or server-side conversion endpoint.
- `components/StudioReuse.tsx` — list running Lightning studios and reuse before creating new ones.
- One route/page to wire them (e.g., `/preview`) and a lightweight backend proxy for tree listing and parquet conversion (minimal, non-breaking).

Scope: new frontend files + one small backend proxy route for tree listing (to avoid CORS/token exposure) and optional parquet conversion. No changes to core training logic.

### 3. Implementation (concrete, ready to apply)

Create directories:
```bash
mkdir -p /opt/axentx/vanguard/src/frontend/lib
mkdir -p /opt/axentx/vanguard/src/frontend/components
```

#### `src/frontend/lib/fileCache.ts`
```ts
const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h
const MAX_LOCAL_CACHE_ENTRIES = 20;

export interface FileListCache {
  repo: string;
  dateFolder: string;
  files: string[];
  fetchedAt: number;
}

function cacheKey(repo: string, dateFolder: string): string {
  return `vanguard:filelist:${repo}:${dateFolder}`;
}

function lruKey(): string {
  return `vanguard:filelist:lru`;
}

function touchLru(key: string) {
  try {
    const raw = localStorage.getItem(lruKey());
    const list = raw ? JSON.parse(raw) as string[] : [];
    const idx = list.indexOf(key);
    if (idx > -1) list.splice(idx, 1);
    list.unshift(key);
    // keep bounded
    while (list.length > MAX_LOCAL_CACHE_ENTRIES) {
      const removed = list.pop();
      if (removed) localStorage.removeItem(removed);
    }
    localStorage.setItem(lruKey(), JSON.stringify(list));
  } catch {}
}

export function loadFileListCache(repo: string, dateFolder: string): FileListCache | null {
  try {
    const raw = localStorage.getItem(cacheKey(repo, dateFolder));
    if (!raw) return null;
    const item = JSON.parse(raw) as FileListCache;
    if (Date.now() - item.fetchedAt > CACHE_TTL_MS) {
      localStorage.removeItem(cacheKey(repo, dateFolder));
      return null;
    }
    touchLru(cacheKey(repo, dateFolder));
    return item;
  } catch {
    return null;
  }
}

export function saveFileListCache(repo: string, dateFolder: string, files: string[]): FileListCache {
  const item: FileListCache = { repo, dateFolder, files, fetchedAt: Date.now() };
  const key = cacheKey(repo, dateFolder);
  localStorage.setItem(key, JSON.stringify(item));
  touchLru(key);
  return item;
}
```

#### `src/frontend/lib/cdnLoader.ts`
```ts
export function cdnUrl(repo: string, filePath: string): string {
  // Public datasets: no auth required
  return `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
}

export async function fetchCdnText(repo: string, filePath: string): Promise<string> {
  const url = cdnUrl(repo, filePath);
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
  return await res.text();
}

export interface PromptResponsePair {
  prompt: string;
  response: string;
  sourceFile: string;
}

export function tryProjectToPairs(raw: string, sourceFile: string): PromptResponsePair[] {
  const pairs: PromptResponsePair[] = [];
  const lines = raw.split("\n").filter((l) => l.trim());
  for (const line of lines) {
    try {
      const obj = JSON.parse(line);
      if (obj.prompt && obj.response) {
        pairs.push({ prompt: String(obj.prompt), response: String(obj.response), sourceFile });
      } else if (obj.messages && Array.isArray(obj.messages)) {
        const user = obj.messages.filter((m: any) => m.role === "user").pop();
        const assistant = obj.messages.filter((m: any) => m.role === "assistant").pop();
        if (user && assistant) {
          pairs.push({
            prompt: typeof user.content === "string" ? user.content : JSON.stringify(user.content),
            response: typeof assistant.content === "string" ? assistant.content : JSON.stringify(assistant.content),
            sourceFile,
          });
        }
      } else if (obj.text && typeof obj.text === "string") {
        pairs.push({ prompt: obj.text, response: "", sourceFile });
      }
    } catch {
      // skip non-json lines
    }
  }
  return pairs;
}
```

#### `src/frontend/components/DataPreview.tsx`
```tsx
import React, { useEffect, useState } from "react";
import { loadFileListCache, saveFileListCache, FileListCache } from "../lib/fileCache";
import { fetchCdnText, tryProjectToPairs, PromptResponsePair } from "../lib/cdnLoader";

interface DataPreviewProps {
  repo: string;
  dateFolder: string;
}

export const DataPreview: React.FC<DataPreviewProps> = ({ repo, dateFolder }) => {
  const [cache, setCache] = useState<FileListCache | null>(null);
  const [pairs, setPairs] = useState<PromptResponsePair[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const c = loadFileListCache(repo, dateFolder);
    if (c) setCache(c);
  }, [repo, dateFolder]);

  const refreshList = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/list-tree?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}&recursive=false`);
      if (!res.ok) throw new Error("List tree failed");
      const { files } = await res.json();
      const saved = saveFileListCache(repo, dateFolder, files);
      setCache(saved);
    } catch (e: any) {
      setError(e.message || "Failed to list files");
    } finally {
      setLoading(false);
    }
  };

  const sampleFiles = (count = 6) => {
    if (!cache?.files.length) return [];
    const candidates = cache.files.filter((f) => /\.(jsonl?|parquet)$/i.test(f));
    return (candidates
