# Costinel / backend

## Final Decision  
**Ship a CDN-first “Top Hub” signal panel** in <2 hours:  
- Add a small, versioned `top-hub.json` artifact (committed to repo + mirrored to HF dataset `resolve/main/`).  
- Expose via `GET /api/top-hub` (backend) with CDN-first fetch, short timeout, and local fallback.  
- Minimal frontend panel that calls `/api/top-hub`, renders hub name, score, and up to 3 related docs, with graceful empty/failure UI.  
- Add a daily cron (bash) that regenerates the artifact via `knowledge-rag` and commits changes.  

This removes model compute and HF API pressure at runtime, uses proven patterns (top-connected hub + CDN bypass + pre-list once), and is fully resilient.

---

## Concrete Implementation Plan

### 1. Artifact and paths
- Generate **`knowledge-rag/top-hubs/latest.json`** (canonical) and copy to **`public/signals/top-hub.json`** (static fallback).  
- JSON shape:
  ```json
  {
    "hub": "MOC",
    "score": 0.0,
    "summary": "Short rationale",
    "related": [
      { "title": "Doc A", "url": "/docs/a" },
      { "title": "Doc B", "url": "/docs/b" }
    ],
    "updatedAt": "2025-01-01T00:00:00Z",
    "version": "2025-01-01",
    "sourcePath": "knowledge-rag/top-hubs/latest.json"
  }
  ```
- CDN primary: `https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub.json` (or your org dataset path).  
- Local static fallback: `/signals/top-hub.json` (served by static middleware) and repo fallback at `knowledge-rag/top-hubs/latest.json`.

---

### 2. Backend (`/api/top-hub`)

**File:** `backend/src/routes/topHub.js` (Node/Express)

```js
import express from 'express';
import fetch from 'node-fetch';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = express.Router();

const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub.json';
const REPO_FALLBACK = path.join(__dirname, '../../knowledge-rag/top-hubs/latest.json');
const STATIC_FALLBACK = path.join(__dirname, '../../public/signals/top-hub.json');

const CACHE_TTL_MS = 60 * 1000; // 60s
let cache = null;
let cacheAt = 0;

function readLocalFallback() {
  // Prefer repo copy, then static copy
  try {
    const raw = fs.readFileSync(REPO_FALLBACK, 'utf8');
    return JSON.parse(raw);
  } catch {
    try {
      const raw = fs.readFileSync(STATIC_FALLBACK, 'utf8');
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }
}

async function fetchTopHub() {
  // In-memory cache
  if (cache && Date.now() - cacheAt < CACHE_TTL_MS) {
    return cache;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2000);

  try {
    const res = await fetch(CDN_URL, { signal: controller.signal });
    clearTimeout(timeout);
    if (res.ok) {
      const data = await res.json();
      cache = data;
      cacheAt = Date.now();
      return data;
    }
  } catch {
    // CDN failed — fall through
  } finally {
    clearTimeout(timeout);
  }

  // Local fallback
  const local = readLocalFallback();
  if (local) {
    cache = local;
    cacheAt = Date.now();
    return local;
  }

  throw new Error('Top hub unavailable');
}

router.get('/api/top-hub', async (req, res) => {
  try {
    const data = await fetchTopHub();
    res.json({
      hub: data.hub || 'MOC',
      score: Number(data.score) || 0,
      related: Array.isArray(data.related) ? data.related.slice(0, 3) : [],
      updatedAt: data.updatedAt || null,
      source: data.sourcePath || 'knowledge-rag',
    });
  } catch (err) {
    res.status(503).json({
      error: 'Top hub unavailable',
      hub: 'MOC',
      score: 0,
      related: [],
      updatedAt: null,
      source: 'fallback',
    });
  }
});

export default router;
```

**Register route** (e.g., in `backend/src/app.js` or server entry):

```js
import topHubRouter from './routes/topHub.js';
app.use(topHubRouter);
```

---

### 3. Frontend panel (React)

**File:** `frontend/components/TopHubPanel.tsx`

```tsx
import { useEffect, useState } from 'react';

interface RelatedDoc {
  title: string;
  url?: string;
}

interface TopHubPayload {
  hub: string;
  score: number;
  related: RelatedDoc[];
  updatedAt: string | null;
  source: string;
}

export default function TopHubPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);

    fetch('/api/top-hub', { signal: controller.signal })
      .then((r) => r.json())
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch(() => {
        setData({ hub: 'MOC', score: 0, related: [], updatedAt: null, source: 'fallback' });
        setLoading(false);
      })
      .finally(() => clearTimeout(timeout));
  }, []);

  if (loading) {
    return <div className="p-3 text-sm text-gray-500">Loading top hub...</div>;
  }

  return (
    <div className="rounded border p-3 bg-white shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-gray-900">Top Hub</h3>
        <span className="text-xs text-gray-400">{data?.source}</span>
      </div>
      <div className="mt-1">
        <p className="text-2xl font-bold text-blue-600">{data?.hub}</p>
        <p className="text-xs text-gray-500">score: {data?.score}</p>
      </div>
      {data?.related && data.related.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm">
          {data.related.map((doc, idx) => (
            <li key={idx} className="truncate">
              {doc.url ? (
                <a
                  href={doc.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline"
                >
                  {doc.title}
                </a>
              ) : (
                <span className="text-gray-700">{doc.title}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {data?.updatedAt && (
        <p className="mt-2 text-xs text-gray-400">Updated: {new Date(data.updatedAt).toLocaleDateString()}</p>
      )}
    </div>
  );
}
```

---

### 4.
