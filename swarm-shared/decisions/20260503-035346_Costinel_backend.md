# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time (single API call on Mac), embedded as JSON, and fetched via CDN during runtime.

---

### Architecture (CDN-first)

```
Mac (orchestration)
  └─ list_repo_tree(recursive=False) → top-hub.json
     └─ commit to repo (or sibling repo if hitting 128/hr cap)
           └─ CDN URL: https://huggingface.co/datasets/.../resolve/main/top-hub.json
Frontend (Costinel)
  └─ fetch(CDN_URL) → render SignalPanel
  └─ zero API/auth/rate-limit during runtime
```

---

### Implementation Steps (≤2h)

1. **Orchestration script** (run on Mac) — 1 file, ~30 LOC
   - Use `list_repo_tree(path, recursive=False)` for a single date folder (e.g., `batches/mirror-merged/2026-05-03/`)
   - Compute top-connected hub (simple heuristic: most frequent `source` or filename slug)
   - Emit `top-hub.json` with `{ hub, score, updatedAt, files[] }`
   - Commit to repo (or sibling repo if near 128/hr cap)

2. **Embed file list in training/data pipeline** (if surrogate-1 training uses this)
   - Save file list to `file-list.json`
   - Embed in `train.py` for CDN-only fetches (no HF API during training)

3. **Frontend SignalPanel** (React/Next.js assumed)
   - Add component `TopHubSignalPanel`
   - Fetch from CDN URL with `useSWR` or `fetch` + revalidate
   - Render card with hub name, score, trend, and last updated
   - Graceful fallback if CDN fails (hide panel, no breakage)

4. **Styling & placement**
   - Place in dashboard sidebar or top bar (non-blocking)
   - Use existing design tokens (colors, spacing)

5. **Cron / automation (optional)**
   - If recurring, add cron with `SHELL=/bin/bash` and proper shebang
   - Example: `0 * * * * /bin/bash /opt/axentx/Costinel/scripts/update-top-hub.sh`

---

### Code Snippets

#### 1. Orchestration script (Mac) — `scripts/update-top-hub.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/axentx/costinel-signals"
DATE=$(date +%Y-%m-%d)
FOLDER="batches/mirror-merged/${DATE}"
OUTPUT="top-hub.json"

# Use HF API once to list files in one date folder (non-recursive)
python3 - <<PY
import os, json, datetime
from huggingface_hub import list_repo_tree

repo = os.getenv("HF_REPO", "$REPO")
folder = os.getenv("FOLDER", "$FOLDER")

# Single API call
tree = list_repo_tree(repo_id=repo, path=folder, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith('.parquet')]

# Simple heuristic: pick most frequent slug pattern as top hub
from collections import Counter
slugs = [f.split('/')[-1].replace('.parquet','') for f in files]
counts = Counter(slugs)
top_hub, score = counts.most_common(1)[0] if counts else ("MOC", 0)

out = {
    "hub": top_hub,
    "score": score,
    "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
    "files": files[:20]  # sample
}

with open("$OUTPUT", "w") as f:
    json.dump(out, f, indent=2)
print(f"Generated {OUTPUT}: {top_hub} (score={score})")
PY

# Commit (respect HF commit cap: spread across siblings if needed)
git add "$OUTPUT"
git commit -m "chore: update top-hub signal for ${DATE}"
git push
```

Make executable:

```bash
chmod +x scripts/update-top-hub.sh
```

---

#### 2. Frontend SignalPanel — `components/TopHubSignalPanel.tsx`

```tsx
'use client';

import useSWR from 'swr';

const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub.json';

interface TopHubData {
  hub: string;
  score: number;
  updatedAt: string;
  files: string[];
}

export default function TopHubSignalPanel() {
  const { data, error } = useSWR<TopHubData>(CDN_URL, (url) =>
    fetch(url).then((res) => {
      if (!res.ok) throw new Error('CDN fetch failed');
      return res.json();
    }),
    { revalidateOnFocus: false, refreshInterval: 1000 * 60 * 10 } // 10m
  );

  if (error || !data) return null; // non-blocking: hide on failure

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-muted-foreground">Top Hub</span>
        <span className="text-xs text-muted-foreground">
          {new Date(data.updatedAt).toLocaleDateString()}
        </span>
      </div>
      <div className="mt-2">
        <p className="text-2xl font-bold">{data.hub}</p>
        <p className="text-sm text-muted-foreground">
          {data.score} active signals
        </p>
      </div>
    </div>
  );
}
```

---

#### 3. Place in dashboard — `app/dashboard/page.tsx` (example)

```tsx
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

export default function DashboardPage() {
  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold">Cost Analytics</h1>
        <TopHubSignalPanel />
      </div>
      {/* rest of dashboard */}
    </div>
  );
}
```

---

### Cron setup (if recurring)

```bash
# crontab -e
SHELL=/bin/bash
0 * * * * /bin/bash /opt/axentx/Costinel/scripts/update-top-hub.sh >> /var/log/costinel-top-hub.log 2>&1
```

---

### Notes & Safeguards

- **Zero runtime API calls**: Frontend fetches only from CDN (`resolve/main/`), bypassing HF auth/rate limits.
- **HF commit cap**: If nearing 128/hr, modify script to hash slug and pick sibling repo deterministically.
- **Graceful degradation**: Panel hides on fetch error — non-blocking.
- **Build-time embedding**: For training pipelines, embed `file-list.json` produced by same script to enable CDN-only data loading in `train.py`.
