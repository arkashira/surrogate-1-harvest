# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time. Runtime dashboard makes **zero HF API calls**.

### Why this is highest-value (<2h)
- Directly applies **top-hub doc insight** and **HF CDN bypass** patterns.
- Non-blocking: panel fails open (empty state) if CDN fetch fails; no impact on core cost views.
- Build-time file-list + CDN fetch eliminates rate-limit risk and keeps runtime lightweight.
- Reuses existing dashboard layout patterns; minimal new code.

---

### Implementation Steps (timed)

1. **Create build-time file list** (5 min)  
   - Add `scripts/generate-top-hub-list.js` that runs on CI (or locally) and outputs `public/data/top-hub-files.json`.  
   - Uses `list_repo_tree(path='knowledge-rag/hubs', recursive=False)` once per day (or per build) and saves filenames + CDN URLs.

2. **Add baked panel data file** (5 min)  
   - Add `public/data/top-hub-latest.json` (committed by CI) with shape:
     ```json
     {
       "hub": "MOC",
       "score": 0.94,
       "summary": "Multi‑modal orchestration core — highest connectivity across cost‑governance signals.",
       "updated": "2026-05-03T04:00:00Z",
       "cdnUrl": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/moc.json"
     }
     ```

3. **Create React panel component** (40 min)  
   - Add `src/components/TopHubSignalPanel.tsx`.  
   - On mount: `fetch('/data/top-hub-latest.json')` (CDN path; falls back to bundled file).  
   - Render card with hub name, score meter, summary, and “View insights” link.  
   - Error boundary: silent no-op if fetch fails (non-blocking).

4. **Wire into dashboard layout** (15 min)  
   - Import and place panel in the sidebar/above fold of `src/pages/Dashboard.tsx`.  
   - Ensure responsive behavior (col-span on desktop, full width on mobile).

5. **Add build script + CI step** (10 min)  
   - Add npm script `"build:top-hub": "node scripts/generate-top-hub-list.js"`.  
   - CI runs this before `npm run build` and commits updated `public/data/top-hub-latest.json` (or injects via env).

6. **Tests & polish** (15 min)  
   - Add simple unit test for panel render states (loading, data, error).  
   - Verify Lighthouse performance impact negligible (<10ms main-thread).

---

### Code Snippets

#### 1. Build script (scripts/generate-top-hub-list.js)
```js
#!/usr/bin/env node
// Generate top-hub file list and latest metadata for CDN-first panel
// Uses huggingface_hub via REST to avoid pyarrow/schema issues; CDN-only runtime.

const fs = require('fs');
const path = require('path');
const https = require('https');

const REPO = 'axentx/knowledge-rag';
const HUBS_PATH = 'hubs';
const OUT_DIR = path.join(__dirname, '..', 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub-latest.json');

if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });

function httpsGet(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      let data = '';
      res.on('data', (chunk) => data += chunk);
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

async function run() {
  try {
    // List top-level files in hubs/ (non-recursive) — single API call
    const treeRes = await httpsGet(`https://huggingface.co/api/datasets/${REPO}/tree?path=${HUBS_PATH}&recursive=false`);
    const tree = JSON.parse(treeRes);

    // Pick most recent JSON file by path (simple heuristic: highest ctime or name)
    const jsonFiles = tree
      .filter((t) => t.type === 'file' && t.path.endsWith('.json'))
      .sort((a, b) => b.lastModified.localeCompare(a.lastModified));

    if (jsonFiles.length === 0) {
      console.warn('No hub JSON files found; skipping top-hub update.');
      return;
    }

    const latest = jsonFiles[0];
    const cdnUrl = `https://huggingface.co/datasets/${REPO}/resolve/main/${latest.path}`;

    // Fetch minimal metadata from CDN (no auth, no API rate limit)
    const raw = await httpsGet(cdnUrl);
    const meta = JSON.parse(raw);

    const out = {
      hub: meta.hub || path.basename(latest.path, '.json'),
      score: meta.score || 0.0,
      summary: meta.summary || 'Top hub insight available.',
      updated: latest.lastModified || new Date().toISOString(),
      cdnUrl
    };

    fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 2));
    console.log('Updated top-hub metadata:', out);
  } catch (err) {
    console.error('Failed to update top-hub metadata:', err.message);
    // Do not fail build; keep existing file.
  }
}

run();
```

#### 2. React panel (src/components/TopHubSignalPanel.tsx)
```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface HubSignal {
  hub: string;
  score: number;
  summary: string;
  updated: string;
  cdnUrl: string;
}

const TopHubSignalPanel: React.FC = () => {
  const [signal, setSignal] = useState<HubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/data/top-hub-latest.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data) => mounted && setSignal(data))
      .catch(() => {
        // Non-blocking: silently skip if CDN/bundled file unavailable
      })
      .finally(() => mounted && setLoading(false));
    return () => {
      mounted = false;
    };
  }, []);

  if (loading && !signal) return null; // fail-open, non-blocking
  if (!signal) return null;

  return (
    <div className="top-hub-panel card">
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <span className="top-hub-name">{signal.hub}</span>
      </div>
      <div className="top-hub-score" title="Connectivity score">
        <div className="score-meter">
          <div className="score-fill" style={{ width: `${Math.max(0, Math.min(100, signal.score * 100))}%` }} />
        </div>
        <span className="score-value">{(signal.score * 100).toFixed(0)}%</span>
      </div>
      <p className="top-hub-summary">{signal.summary}</p>
      <div className="top-hub-footer">
        <a href={signal.cdnUrl} target="_blank" rel="noopener noreferrer" className="top-hub-link">
          View insights →
        </a>
        <time className="top-hub-updated" dateTime={signal.updated}>
          Updated {new Date(signal.updated).toLocaleDateString()}
        </time>
      </div>
    </div>
  );
};

export default TopHubSignalPanel;
```

#### 3. Minimal CSS (src/components/TopHubSignalPanel.css)
```css
.top-hub-panel {
  padding: 1rem;
  border-radius
