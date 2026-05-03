# vanguard / frontend

## Final synthesized answer

### Diagnosis (merged)
- The frontend re-triggers authenticated `list_repo_tree` on every session/page load, burning HF API quota and risking 429s.
- No persisted `(repo, dateFolder) → file-list` manifest exists in frontend or via a lightweight endpoint, forcing repeated round-trips.
- Data-selection UI blocks or shows stale/empty states during rate-limit windows; no CDN-only path list is available, so enumeration requires auth when it could be pre-computed.
- Training/inference likely uses `load_dataset(streaming=True)` or per-file loads that hit HF API instead of CDN-only fetches.
- No local cache/fallback when HF API is rate-limited; mixed-schema ingestion paths can leak into frontend expectations.

### Goal
Eliminate authenticated HF API calls for file enumeration and per-file fetches during normal operation. Provide a persisted manifest, a tiny API to serve it, CDN-first fetches, and a generator run by the orchestrator/Mac. Keep frontend changes minimal and production-safe.

---

### Implementation (concrete, prioritized)

#### 1. Generator (run once per dateFolder by orchestrator/Mac)
Location: `/opt/axentx/vanguard/frontend/scripts/generate-file-list.js`

```js
#!/usr/bin/env node
// Usage: node generate-file-list.js <repo> <dateFolder> [outFile]
// Example: node generate-file-list.js datasets/your-repo 2026-05-03 src/lib/manifest.json
const { HfApi } = require("@huggingface/hub");
const fs = require("fs");
const path = require("path");

async function main() {
  const repo = process.argv[2];
  const dateFolder = process.argv[3];
  const outFile = path.resolve(process.argv[4] || "src/lib/manifest.json");

  if (!repo || !dateFolder) {
    console.error("Usage: node generate-file-list.js <repo> <dateFolder> [outFile]");
    process.exit(1);
  }

  const api = new HfApi();
  const tree = await api.listRepoTree(repo, path.join(dateFolder), { recursive: true });
  const files = (tree.files || [])
    .filter((f) => /\.(parquet|jsonl|json)$/.test(f.path))
    .map((f) => ({
      path: f.path,
      size: f.size,
      lfs: !!f.lfs,
    }));

  const manifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };

  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.writeFileSync(outFile, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written to ${outFile} (${files.length} files)`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

Make executable and run:
```bash
chmod +x /opt/axentx/vanguard/frontend/scripts/generate-file-list.js
cd /opt/axentx/vanguard/frontend
node scripts/generate-file-list.js datasets/your-repo 2026-05-03 src/lib/manifest.json
```

---

#### 2. Lightweight API endpoint (serves manifest)
Location: `/opt/axentx/vanguard/frontend/src/routes/api/file-list.js`

```js
const express = require("express");
const fs = require("fs");
const path = require("path");

const router = express.Router();

function findManifest(repo, dateFolder) {
  const manifestPath = path.resolve(__dirname, "../../../lib/manifest.json");
  if (!fs.existsSync(manifestPath)) return null;
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  if (manifest.repo !== repo || manifest.dateFolder !== dateFolder) return null;
  return manifest;
}

router.get("/file-list", (req, res) => {
  const { repo, date } = req.query;
  if (!repo || !date) {
    return res.status(400).json({ error: "repo and date query params required" });
  }

  const manifest = findManifest(repo, date);
  if (!manifest) {
    return res.status(404).json({
      error: "manifest not found or mismatch — run generate-file-list.js",
    });
  }

  res.json({
    repo: manifest.repo,
    dateFolder: manifest.dateFolder,
    generatedAt: manifest.generatedAt,
    files: manifest.files.map((f) => f.path),
  });
});

module.exports = router;
```

Wire into your server (example):
```js
// In your main server file
const fileListRoute = require("./src/routes/api/file-list");
app.use("/api", fileListRoute);
```

---

#### 3. Frontend: manifest manager + CDN-first fetches
Location: `/opt/axentx/vanguard/src/lib/hf-cdn.ts`

```ts
const CDN_ROOT = "https://huggingface.co/datasets";

export function cdnUrl(repo: string, filePath: string): string {
  return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

export async function fetchCdnArrayBuffer(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  return res.arrayBuffer();
}
```

Location: `/opt/axentx/vanguard/src/features/data/useDataManifest.ts`

```ts
import { useEffect, useState, useCallback } from "react";

type FileEntry = { path: string };

type Manifest = {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  files: FileEntry[];
};

export function useDataManifest() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadManifest = useCallback(async (repo: string, dateFolder: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/file-list?repo=${encodeURIComponent(repo)}&date=${encodeURIComponent(dateFolder)}`);
      if (!res.ok) throw new Error("Failed to load file list");
      const data: Manifest = await res.json();
      setManifest(data);
      // persist for offline/fallback
      try {
        localStorage.setItem(`manifest:${repo}:${dateFolder}`, JSON.stringify(data));
      } catch (e) {
        // ignore storage errors
      }
    } catch (err: any) {
      // try fallback
      try {
        const cached = localStorage.getItem(`manifest:${repo}:${dateFolder}`);
        if (cached) {
          setManifest(JSON.parse(cached));
        } else {
          throw err;
        }
      } catch (fallbackErr) {
        setError(fallbackErr instanceof Error ? fallbackErr.message : String(fallbackErr));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const getCdnUrls = useCallback(
    (repo: string) => {
      if (!manifest) return [];
      return manifest.files.map((f) => ({
        path: f.path,
        url: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(f.path)}`,
      }));
    },
    [manifest]
  );

  return {
    manifest,
    loading,
    error,
    loadManifest,
    getCdnUrls,
  };
}
```

---

#### 4. Frontend: update TrainPanel (example)
Location: `/opt/axentx/vanguard/src/features/training/TrainPanel.tsx`

```tsx
import React, { useEffect, useState } from "react";
import { useDataManifest } from "../data/useDataManifest";

export function TrainPanel() {
  const [repo, setRepo] = useState("datasets/your-repo");
  const [date, setDate] = useState("2026-05-03");
  const { loadManifest, loading, error, getCdnUrls
