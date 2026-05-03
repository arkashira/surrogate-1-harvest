# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time into a static JSON file served via CDN; frontend hydrates from `/data/top-hub.json`.

### Architecture (CDN-first)
- **Build step** (`scripts/build-top-hub.js`): runs on CI or local pre-build. Uses a single `list_repo_tree` call (rate-limit safe) → fetches latest `knowledge-rag/top-hub.json` via CDN → writes `public/data/top-hub.json`.
- **Runtime**: frontend fetches `/data/top-hub.json` (CDN, no auth, no API). Falls back to cached/empty state if unavailable.
- **No HF API during app runtime** → avoids 429s and quota usage.

### Files to add/modify
- `public/data/top-hub.json` (generated)
- `scripts/build-top-hub.js`
- `src/components/TopHubSignalPanel.tsx`
- `src/pages/Dashboard.tsx` (or equivalent) — mount panel
- `package.json` scripts: add `"build:top-hub": "node scripts/build-top-hub.js"`

---

## 1) Build script (CDN fetcher)

`scripts/build-top-hub.js`
```js
#!/usr/bin/env node
/**
 * Build-time script to produce public/data/top-hub.json
 * Uses CDN to avoid runtime HF API calls.
 */
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import axios from 'axios';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(ROOT, 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

const HF_REPO = 'axentx/knowledge-rag';
const HF_TOKEN = process.env.HF_TOKEN || '';

async function run() {
  try {
    await fs.mkdir(OUT_DIR, { recursive: true });

    // 1) list root tree (non-recursive) to find top-hub.json
    const treeRes = await axios.get(
      `https://huggingface.co/api/datasets/${HF_REPO}/tree`,
      {
        params: { recursive: false },
        headers: HF_TOKEN ? { Authorization: `Bearer ${HF_TOKEN}` } : {},
        timeout: 15000,
      }
    );

    const entry = (treeRes.data || []).find((f) => f.path === 'top-hub.json');
    if (!entry) {
      await fs.writeFile(
        OUT_FILE,
        JSON.stringify({ available: false, reason: 'top-hub.json not found in repo' }, null, 2)
      );
      console.log('top-hub.json not found in repo; wrote placeholder.');
      return;
    }

    // 2) Download via CDN (no auth required)
    const cdnUrl = `https://huggingface.co/datasets/${HF_REPO}/resolve/main/top-hub.json`;
    const dataRes = await axios.get(cdnUrl, { timeout: 30000 });
    const payload = dataRes.data || {};

    // Normalize minimal shape expected by frontend
    const normalized = {
      available: true,
      hub: payload.hub || payload.top_hub || 'MOC',
      score: Number(payload.score || payload.strength || 0),
      summary: payload.summary || payload.description || '',
      updated: payload.updated || payload.ts || new Date().toISOString(),
      links: Array.isArray(payload.links) ? payload.links.slice(0, 6) : [],
    };

    await fs.writeFile(OUT_FILE, JSON.stringify(normalized, null, 2));
    console.log('Wrote top-hub.json to public/data/top-hub.json');
  } catch (err) {
    // Non-blocking: write safe fallback so build doesn't fail
    await fs.writeFile(
      OUT_FILE,
      JSON.stringify({ available: false, reason: err.message || 'unknown' }, null, 2)
    );
    console.warn('Failed to fetch top-hub data (non-blocking):', err.message);
  }
}

run();
```

Make executable:
```bash
chmod +x scripts/build-top-hub.js
```

---

## 2) Placeholder data (committed)

`public/data/top-hub.json`
```json
{
  "available": false,
  "reason": "built by CI"
}
```

---

## 3) React component

`src/components/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface TopHubData {
  available: boolean;
  hub?: string;
  score?: number;
  summary?: string;
  updated?: string;
  links?: Array<{ url: string; title?: string }>;
  reason?: string;
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('fetch failed'))))
      .then((json) => {
        if (mounted) {
          setData(json);
          setLoading(false);
        }
      })
      .catch(() => {
        if (mounted) {
          setData({ available: false, reason: 'network' });
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <div className="skeleton"></div>
      </div>
    );
  }

  if (!data || !data.available) {
    // Non-blocking: render minimal muted placeholder
    return (
      <div className="top-hub-panel muted">
        <span className="label">Top Hub</span>
        <span className="value">—</span>
      </div>
    );
  }

  return (
    <div className="top-hub-panel" title={`Updated: ${data.updated}`}>
      <div className="header">
        <span className="label">Top Hub</span>
        {typeof data.score === 'number' && (
          <span className="score" title="strength">{data.score.toFixed(2)}</span>
        )}
      </div>
      <div className="hub">{data.hub || '—'}</div>
      {data.summary && <p className="summary">{data.summary}</p>}
      {Array.isArray(data.links) && data.links.length > 0 && (
        <ul className="links">
          {data.links.map((l, i) => (
            <li key={i}>
              <a href={l.url} target="_blank" rel="noopener noreferrer">
                {l.title || l.url}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

---

## 4) Styles

`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 10px;
  padding: 14px 16px;
  color: #e2e8f0;
  font-family: inherit;
  min-height: 80px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.top-hub-panel .header {
  display: flex;
  align-items: baseline;
 
