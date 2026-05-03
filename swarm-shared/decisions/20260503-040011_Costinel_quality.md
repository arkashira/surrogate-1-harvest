# Costinel / quality

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time: fetch once, emit static JSON (unified approach)

Use the **Node script** (Candidate 2) for richer fallback logic, but **keep the curl-based shell script** (Candidate 1) as the primary CI step to avoid auth/token requirements and reduce dependencies.

**Primary CI/build step** (recommended):

```bash
# scripts/bake-top-hub.sh
#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"
HUB_REPO="axentx/knowledge-rag"
HUB_PATH="graph/top-hub.json"

mkdir -p "${OUT_DIR}"

if curl -fsSL --retry 2 --max-time 10 \
  "https://huggingface.co/datasets/${HUB_REPO}/resolve/main/${HUB_PATH}" \
  -o "${OUT_FILE}.tmp"; then
  mv "${OUT_FILE}.tmp" "${OUT_FILE}"
  echo "✅ Baked top-hub: $(cat ${OUT_FILE})"
else
  echo '{"hub":"MOC","score":0.0,"updated":"2026-04-27","offline":true}' > "${OUT_FILE}"
  echo "⚠️  CDN fetch failed — baked offline stub"
fi
```

**Fallback Node script** (optional, for local dev or richer selection):

```js
// scripts/build-top-hub.js
#!/usr/bin/env node
import fs from "fs/promises";
import path from "path";

const OUT_DIR = "public/data";
const OUT_FILE = "top-hub.json";

async function main() {
  try {
    // Try curl first (same as CI)
    const { execSync } = await import("child_process");
    const out = execSync(
      `curl -fsSL --retry 2 --max-time 10 \
        "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/graph/top-hub.json"`,
      { encoding: "utf8" }
    ).trim();
    const payload = JSON.parse(out);
    await fs.mkdir(OUT_DIR, { recursive: true });
    await fs.writeFile(path.join(OUT_DIR, OUT_FILE), JSON.stringify(payload, null, 2));
    console.log("Top-hub baked:", payload);
  } catch {
    // Fallback: safe defaults so build never fails
    await fs.mkdir(OUT_DIR, { recursive: true });
    await fs.writeFile(
      path.join(OUT_DIR, OUT_FILE),
      JSON.stringify({ hub: "MOC", score: 0, updated: "2026-04-27", offline: true })
    );
    console.warn("Top-hub fallback used");
  }
}

main();
```

**Add to build** (Dockerfile or package.json):

```dockerfile
RUN ./scripts/bake-top-hub.sh
```

or

```json
"scripts": {
  "prebuild": "bash ./scripts/bake-top-hub.sh",
  "build": "next build"
}
```

---

### 2) Frontend: lightweight non-blocking panel (unified)

Use Candidate 1’s component (clean, minimal, non-blocking) with Candidate 2’s fallback behavior baked in.

`src/components/TopHubSignalPanel.tsx`:

```tsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface HubData {
  hub: string;
  score: number;
  updated: string;
  offline?: boolean;
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);

  useEffect(() => {
    // Non-blocking: fetch baked JSON from public path
    fetch("/data/top-hub.json", { cache: "no-store" })
      .then((r) => r.json())
      .then(setHub)
      .catch(() => {
        setHub({ hub: "MOC", score: 0, updated: "2026-04-27", offline: true });
      });
  }, []);

  if (!hub) return null; // don't block render

  return (
    <div className="top-hub-panel" title={`Updated ${hub.updated}`}>
      <span className="label">Top Hub</span>
      <span className="value">{hub.hub}</span>
      {typeof hub.score === "number" && hub.score > 0 && (
        <span className="score">{Math.round(hub.score * 100)}%</span>
      )}
      {hub.offline && <span className="offline-badge">offline</span>}
    </div>
  );
}
```

`src/components/TopHubSignalPanel.css`:

```css
.top-hub-panel {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0.6rem;
  border-radius: 6px;
  background: rgba(16, 185, 129, 0.08);
  border: 1px solid rgba(16, 185, 129, 0.15);
  color: #059669;
  font-size: 0.8rem;
  font-weight: 600;
}

.top-hub-panel .label {
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 0.7rem;
  opacity: 0.7;
}

.top-hub-panel .value {
  margin-left: 0.25rem;
}

.top-hub-panel .score {
  margin-left: 0.5rem;
  font-variant-numeric: tabular-nums;
  opacity: 0.9;
}

.top-hub-panel .offline-badge {
  margin-left: 0.5rem;
  font-size: 0.65rem;
  opacity: 0.6;
  text-transform: uppercase;
}
```

---

### 3) Mount panel in dashboard (unified)

Insert into a prominent but non-blocking location:

```tsx
// src/pages/Dashboard.tsx (or similar)
import TopHubSignalPanel from "@/components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <h1>Cloud Cost Governance</h1>
        <TopHubSignalPanel />
      </header>
      {/* ...rest of dashboard */}
    </div>
  );
}
```

---

### 4) CI automation (keep data fresh)

Add a scheduled job to refresh the baked file daily:

```yaml
# .github/workflows/bake-top-hub.yml
name: Bake Top-Hub
on:
  schedule:
    - cron: "0 6 * * *"   # daily 06:00 UTC
  workflow_dispatch:

jobs:
  bake:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./scripts/bake-top-hub.sh
      - uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "chore: refresh top-hub baked data"
          file_pattern: "public/data/top-hub.json"
```

---

### 5) Acceptance checklist (quick QA)

- [ ] `scripts/bake-top-hub.sh` is executable and runs without error.
- [ ] Build produces `public/data/top-hub.json` (valid JSON).
- [ ] Panel renders without blocking page load and shows hub + score.
- [ ] Panel gracefully handles missing/invalid JSON (offline stub).
- [ ] No runtime HF API calls (verify network tab).

---

**Estimated effort**: ~1.5h (including tests and commit).  
**Value**: Surfaces top-hub insight immediately for Costinel users, aligns with knowledge-rag
