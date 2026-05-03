# Costinel / backend

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — **CDN-first, rate-limit-safe, zero HF API calls during render**.

---

## Why This Is Highest Value
- **Immediate business value**: dashboard shows actionable cost signals without runtime HF API calls.
- **Low risk**: read-only, CDN-only fetches, no execution or mutations.
- **Fits <2h**: small backend endpoint + frontend panel + static file list baked at build/deploy.
- **Leverages proven patterns**: top-hub insight, CDN bypass, pre-generated file list, read-only signals.

---

## Implementation Plan (Concrete + Actionable)

### 1) File list pre-generation (Mac orchestration / CI)

Run once or in CI after knowledge-rag produces graph artifacts.

```bash
#!/usr/bin/env bash
# scripts/generate-hub-filelist.sh
set -euo pipefail

REPO="axentx/costinel-knowledge"
OUT="static/data/hub-files.json"

python3 - <<PY
import json, os
from huggingface_hub import list_repo_tree

files = list_repo_tree(
    repo_id="$REPO",
    path="enriched",
    repo_type="dataset",
    recursive=True
)
# Keep only parquet; include relative path and date segment for deterministic sorting
items = []
for f in files:
    if f.rfilename.endswith(".parquet"):
        items.append({
            "path": f.rfilename,
            "date": os.path.normpath(f.rfilename).split(os.sep)[1] if os.sep in f.rfilename else ""
        })
# Prefer latest date, then deterministic sort
items.sort(key=lambda x: (x["date"], x["path"]), reverse=True)
os.makedirs(os.path.dirname("$OUT"), exist_ok=True)
with open("$OUT", "w") as f:
    json.dump(items, f, indent=2)
print(f"Wrote {len(items)} files to $OUT")
PY
```

- Commit `static/data/hub-files.json` (small, deterministic).
- Runtime uses only this file + CDN URLs — **zero HF API calls at runtime**.

---

### 2) Backend: CDN-first signal endpoint

Add `/api/signals/top-hub` that:

- Reads `static/data/hub-files.json`.
- Picks latest file (or date from query).
- Streams selected parquet from CDN (`resolve/main/...`) with no Authorization header.
- Projects to `{prompt, response}` (or expected schema) and returns top 3 proposals.

File: `backend/routes/signals.py`

```python
from fastapi import APIRouter, HTTPException
import pyarrow.parquet as pq
import pyarrow as pa
import httpx
import json
import os
from datetime import datetime, timezone

router = APIRouter()

HF_DATASET = "axentx/costinel-knowledge"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"
FILE_LIST = os.getenv("HUB_FILE_LIST", "static/data/hub-files.json")

def _latest_file():
    with open(FILE_LIST) as f:
        files = json.load(f)
    if not files:
        raise HTTPException(status_code=404, detail="No hub files available")
    return files[0]["path"]

def _fetch_parquet_from_cdn(path: str) -> pa.Table:
    url = f"{CDN_ROOT}/{path}"
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return pq.read_table(pa.BufferReader(resp.content))

@router.get("/top-hub")
def top_hub_signals(date: str | None = None, limit: int = 3):
    """
    Return top {limit} actionable signals from the most-connected hub.
    Uses CDN-only fetches; zero HF API calls.
    """
    try:
        if date:
            file_path = f"enriched/{date}/MOC.parquet"
        else:
            file_path = _latest_file()

        table = _fetch_parquet_from_cdn(file_path)

        cols = set(table.column_names)
        if "response" in cols:
            responses = table["response"].to_pylist()
        elif "text" in cols:
            responses = table["text"].to_pylist()
        else:
            raise HTTPException(
                status_code=500,
                detail="No response/text column in hub file"
            )

        cost_keywords = {"cost", "savings", "ri", "reserved", "idle", "rightsizing", "cut", "reduce", "optimize"}
        scored = []
        for r in responses:
            text = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else str(r)
            score = sum(1 for k in cost_keywords if k in text.lower())
            if score > 0:
                scored.append((score, text[:512]))

        scored.sort(key=lambda x: -x[0])
        top_items = [item[1] for item in scored[:limit]]

        return {
            "hub": "MOC",
            "file": file_path,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "signals": top_items,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch hub signals: {exc}")
```

Wire into main app (`backend/main.py`):

```python
from backend.routes import signals
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
```

---

### 3) Frontend: Signal Panel component

Add a lightweight panel to the dashboard (React/Next.js).

File: `frontend/components/TopHubSignalPanel.tsx`

```tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Loader2, AlertCircle } from 'lucide-react';

interface SignalPanelProps {
  date?: string;
}

export default function TopHubSignalPanel({ date }: SignalPanelProps) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchSignals = async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams();
        if (date) params.set('date', date);
        const res = await fetch(`/api/signals/top-hub?${params}`);
        if (!res.ok) throw new Error(await res.text());
        setData(await res.json());
        setError(null);
      } catch (e: any) {
        setError(e.message || 'Failed to load signals');
      } finally {
        setLoading(false);
      }
    };

    fetchSignals();
  }, [date]);

  if (loading) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center py-6">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          Loading hub signals...
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 py-6 text-destructive">
          <AlertCircle className="h-4 w-4" />
          {error}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-base">
          <span>Top-Hub Signals</span>
          <Badge variant="secondary">{data?.hub || 'MOC'}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2 text-sm">
          {data?.signals?.map((s: string, i: number) => (
            <li key={i} className="rounded border p-2
