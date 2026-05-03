# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time data pipeline (Mac/CI)
- Single API call (after rate-limit window) to list one date folder:  
  `list_repo_tree("knowledge-rag/top-hub/2026-04-27", recursive=False)`
- Save `top-hub-files.json` to repo (committed) or CI artifact.
- Fetch each file via CDN (`https://huggingface.co/datasets/.../resolve/main/...`) and reduce to `{ hub, score, links, updated_at }`.
- Emit `public/signals/top-hub.json` (minified) with attribution in filename pattern:  
  `batches/mirror-merged/2026-04-27/top-hub-MOC.json`
- CI step (bash):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  SHELL=/bin/bash
  DATE="2026-04-27"
  OUT="public/signals/top-hub.json"
  python scripts/build_top_hub.py --date "$DATE" --out "$OUT"
  ```

---

### 2) Runtime behavior (zero HF API)
- Frontend loads `/signals/top-hub.json` via CDN (static asset).
- If fetch fails → silent no-op (non-blocking).
- Panel renders top hub card with link to knowledge-rag context.

---

### 3) Code changes

#### `public/signals/top-hub.json` (example baked file)
```json
{
  "hub": "MOC",
  "score": 0.94,
  "links": 128,
  "updated_at": "2026-04-27T00:00:00Z",
  "context_url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/2026-04-27/MOC.json"
}
```

#### `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface TopHubSignal {
  hub: string;
  score: number;
  links: number;
  updated_at: string;
  context_url: string;
}

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);

  useEffect(() => {
    // CDN-first, zero HF API at runtime
    fetch("/signals/top-hub.json", { cache: "max-age=3600" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setSignal)
      .catch(() => {
        /* non-blocking: silently ignore */
      });
  }, []);

  if (!signal) return null;

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signal">
      <div className="panel-header">
        <span className="badge">Top Hub</span>
        <time dateTime={signal.updated_at}>
          {new Date(signal.updated_at).toLocaleDateString()}
        </time>
      </div>
      <div className="panel-body">
        <h3>{signal.hub}</h3>
        <p className="score">Relevance {Math.round(signal.score * 100)}%</p>
        <p className="links">{signal.links} connected docs</p>
        <a
          className="cta"
          href={signal.context_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          View context
        </a>
      </div>
    </div>
  );
}
```

#### `src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ef;
  border-radius: 8px;
  padding: 12px 16px;
  background: #fff;
  max-width: 320px;
  font-family: system-ui, -apple-system, sans-serif;
}

.top-hub-panel .panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-size: 12px;
  color: #6b7280;
}

.top-hub-panel .badge {
  background: #1e40af;
  color: #fff;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 600;
  font-size: 11px;
}

.top-hub-panel .panel-body h3 {
  margin: 4px 0;
  font-size: 20px;
  color: #0f172a;
}

.top-hub-panel .score {
  margin: 4px 0;
  font-size: 13px;
  color: #047857;
}

.top-hub-panel .links {
  margin: 4px 0 10px;
  font-size: 13px;
  color: #6b7280;
}

.top-hub-panel .cta {
  display: inline-block;
  font-size: 13px;
  color: #2563eb;
  text-decoration: none;
}

.top-hub-panel .cta:hover {
  text-decoration: underline;
}
```

#### Integrate into dashboard (example)
```tsx
// In your dashboard layout
import TopHubSignalPanel from "@/components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div>
      {/* existing content */}
      <div className="fixed top-4 right-4 z-10">
        <TopHubSignalPanel />
      </div>
    </div>
  );
}
```

---

### 4) Build script (optional, for CI)
#### `scripts/build_top_hub.py`
```python
#!/usr/bin/env python3
"""
Build-time: produce public/signals/top-hub.json from HF CDN.
Run on Mac/CI only (not in browser).
"""
import argparse
import json
import urllib.request
from datetime import datetime

HF_DATASETS_BASE = "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main"

def build(date: str, out_path: str) -> None:
    listing_url = f"{HF_DATASETS_BASE}/top-hub/{date}/"
    # Simple approach: expect a known file pattern; in practice list via HF tree once.
    hub_file = f"{HF_DATASETS_BASE}/top-hub/{date}/MOC.json"
    try:
        with urllib.request.urlopen(hub_file) as resp:
            data = json.load(resp)
    except Exception as e:
        # fallback minimal payload
        data = {"hub": "MOC", "score": 0.0, "links": 0}

    payload = {
        "hub": data.get("hub", "MOC"),
        "score": data.get("score", 0.0),
        "links": data.get("links", 0),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "context_url": hub_file,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    build(date=args.date, out_path=args.out)
```

---

### 5) Acceptance criteria
- Panel appears in top-right of dashboard when `public/signals/top-hub.json` exists.
- No network calls to HuggingFace `/api/` endpoints
