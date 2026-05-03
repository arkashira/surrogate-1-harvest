# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time data pipeline (Mac/CI) — 15min
- Single `list_repo_tree` call to `knowledge-rag/top-hubs/YYYY-MM-DD/` (non-recursive) → save `top-hubs.json`.
- Pick hub with highest `pagerank`/`degree` → emit `top-hub.json`:
  ```json
  {
    "hub": "MOC",
    "score": 0.94,
    "label": "Most-Connected Hub",
    "updated": "2026-05-03",
    "cdnPath": "knowledge-rag/top-hubs/2026-05-03/MOC.json"
  }
  ```
- Commit `public/data/top-hub.json` (or upload to CDN and reference by public URL).

### 2) Frontend component — 45min
Create `src/components/TopHubSignalPanel.tsx`:
- Loads `/data/top-hub.json` (CDN) with `fetch` + `AbortController`.
- Non-blocking: renders skeleton while loading; fails silently to empty.
- Displays hub name, score, freshness, and link to hub detail.

### 3) Integration — 15min
- Add panel to dashboard layout (e.g., sidebar or top-right card).
- Ensure SSR-safe (client-only fetch) and respects `prefers-reduced-motion`.

### 4) Tests & polish — 45min
- Mock CDN response in tests.
- Add Lighthouse perf/no-HF-runtime checks.

---

## Code Snippets

### Build script (mac/CI) — `scripts/build-top-hub.js`
```js
#!/usr/bin/env node
// Usage: node scripts/build-top-hub.js > public/data/top-hub.json
// Requires: HF_TOKEN in env (only during build)
const { HfApi } = require("@huggingface/huggingface-hub");
const fs = require("fs");
const path = require("path");

async function main() {
  const api = new HfApi({ token: process.env.HF_TOKEN });
  const owner = "AXENTX";
  const repo = "knowledge-rag";
  const folder = "top-hubs";
  // list one date folder (non-recursive)
  const trees = await api.listRepoTree(owner, repo, { path: folder, recursive: false });
  // pick latest date subfolder
  const dateFolders = trees
    .filter((t) => t.type === "directory")
    .map((t) => t.path)
    .sort()
    .reverse();
  if (dateFolders.length === 0) throw new Error("No date folders found");
  const latest = dateFolders[0];

  const files = await api.listRepoTree(owner, repo, { path: latest, recursive: false });
  const jsonFiles = files.filter((f) => f.type === "file" && f.path.endsWith(".json"));

  // fetch each hub file via CDN (no auth) and pick top score
  const hubPromises = jsonFiles.map(async (f) => {
    const url = `https://huggingface.co/datasets/${owner}/${repo}/resolve/main/${latest}/${f.path}`;
    const res = await fetch(url).then((r) => r.json());
    return {
      hub: res.hub || path.basename(f.path, ".json"),
      score: res.pagerank ?? res.score ?? 0,
      label: res.label || "Hub",
      cdnPath: `${latest}/${f.path}`,
    };
  });

  const hubs = await Promise.all(hubPromises);
  const top = hubs.sort((a, b) => b.score - a.score)[0];
  if (!top) throw new Error("No hub data");

  const out = {
    hub: top.hub,
    score: Number(top.score.toFixed(3)),
    label: top.label,
    updated: latest,
    cdnPath: top.cdnPath,
  };
  fs.writeFileSync(path.join(__dirname, "..", "public", "data", "top-hub.json"), JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
}

if (require.main === module) main().catch((err) => { console.error(err); process.exit(1); });
```

### React component — `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState, useRef } from "react";
import "./TopHubSignalPanel.css";

interface TopHubData {
  hub: string;
  score: number;
  label: string;
  updated: string;
  cdnPath: string;
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    abortRef.current = new AbortController();
    const load = async () => {
      try {
        const res = await fetch("/data/top-hub.json", {
          signal: abortRef.current.signal,
          cache: "no-cache",
        });
        if (!res.ok) throw new Error("No data");
        const json = (await res.json()) as TopHubData;
        setData(json);
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          // fail silently — panel will render empty
          console.debug("TopHubSignalPanel: no data", err);
        }
      } finally {
        setLoading(false);
      }
    };
    load();
    return () => abortRef.current?.abort();
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel skeleton" aria-busy="true">
        <div className="shimmer" />
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signal">
      <div className="top-hub-header">
        <span className="top-hub-badge">{data.label}</span>
        <time className="top-hub-date" dateTime={data.updated}>{data.updated}</time>
      </div>
      <div className="top-hub-body">
        <strong className="top-hub-name">{data.hub}</strong>
        <div className="top-hub-score" title="Connection strength">
          {Math.round(data.score * 100)}%
        </div>
      </div>
      <a
        className="top-hub-link"
        href={`/knowledge-rag/hubs/${encodeURIComponent(data.hub)}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        View hub details →
      </a>
    </div>
  );
}
```

### Styles — `src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid var(--border, #e6e9ee);
  border-radius: 10px;
  padding: 14px 16px;
  background: linear-gradient(180deg, #fff 0%, #fbfdff 100%);
  max-width: 280px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.top-hub-panel.skeleton {
  height: 80px;
  position: relative;
  overflow: hidden;
  background: #f6f7f9;
}

.top-hub-panel.skeleton .shimmer {
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.6), transparent);
  animation: shimmer 1.5s infinite;
}

@keyframes shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
