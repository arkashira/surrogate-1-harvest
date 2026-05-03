# Costinel / backend

## Final Synthesized Answer

**Highest-Value Incremental Improvement (<2h)**  
Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — **CDN-first, rate-limit-safe, zero API calls during render**.

### Why this is highest value
- Directly applies the **top-hub doc insight** pattern (MOC) and **knowledge-rag** pattern.
- Uses **HF CDN bypass** to avoid 429s during dashboard loads.
- Ships a visible, decision-ready UX element in <2h without touching billing or execution paths (Sense + Signal).

---

## Concrete Implementation Plan

### 1) Pre-list hub file paths once (Mac orchestration)
Run once (or in CI) after knowledge-rag produces updated graph. Embed file list so Lightning/CDN training or runtime fetches never hit `/api/`.

```bash
# Mac orchestration script (run after knowledge-rag)
# Save to /opt/axentx/Costinel/config/hub_file_list.json
python3 <<'PY'
import json, os
from huggingface_hub import list_repo_tree

repo = "axentx/costinel-knowledge"
path = "hubs"
tree = list_repo_tree(repo=repo, path=path, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith(".json")]
# sort by date or connectivity score descending if encoded in name
files.sort(reverse=True)
out = {"repo": repo, "path_prefix": path, "files": files, "generated_at": __import__('datetime').datetime.utcnow().isoformat()}
os.makedirs("config", exist_ok=True)
with open("config/hub_file_list.json", "w") as f:
    json.dump(out, f, indent=2)
print("Saved hub file list:", len(files))
PY
```

Commit `config/hub_file_list.json` to repo (or bake into Docker image). CDN URLs are:
```
https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs/{filename}
```
No Authorization header required; CDN-only fetches bypass `/api/` rate limits.

---

### 2) Backend: FastAPI route to serve top-hub signals
File: `/opt/axentx/Costinel/app/api/top_hub.py`

```python
# app/api/top_hub.py
import asyncio
import aiohttp
import json
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any

router = APIRouter(prefix="/api/top-hub", tags=["top-hub"])

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "hub_file_list.json"
CDN_TEMPLATE = "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs/{filename}"

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    async with session.get(url, timeout=10) as resp:
        if resp.status != 200:
            raise HTTPException(status_code=502, detail=f"CDN fetch failed: {url}")
        return await resp.json()

@router.get("/panel", response_model=Dict[str, Any])
async def get_top_hub_panel(hub_name: str = "MOC", limit: int = 3):
    """
    Returns top actionable signals for a hub.
    Uses CDN-only fetches; zero HuggingFace API calls during render.
    """
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail="Hub file list not found")

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    candidates = [f for f in cfg["files"] if hub_name.lower() in f.lower()] or cfg["files"][:limit]
    urls = [CDN_TEMPLATE.format(filename=fname) for fname in candidates[:limit]]

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_json(session, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    signals = []
    for fname, data in zip(candidates[:limit], results):
        if isinstance(data, Exception):
            continue
        # Expected shape (adjust per your graph output):
        # { "hub": "MOC", "score": 0.94, "proposals": [ { "title": "...", "impact_usd": 1234, "rationale": "..." } ] }
        proposals = data.get("proposals", [])
        if not proposals and "proposal" in data:
            proposals = [data["proposal"]]
        for p in proposals[:3]:
            signals.append({
                "hub": data.get("hub", hub_name),
                "score": data.get("score", 0.0),
                "source_file": fname,
                "proposal": p.get("title", "Untitled"),
                "impact_usd": p.get("impact_usd", 0),
                "rationale": p.get("rationale", ""),
                "actions": p.get("actions", []),
            })

    # Sort by impact_usd desc, then score desc
    signals.sort(key=lambda x: (-x["impact_usd"], -x["score"]))
    return {
        "hub": hub_name,
        "generated_at": cfg.get("generated_at"),
        "top_signals": signals[:limit],
    }
```

Register route in main app (likely `app/main.py` or `app/api/__init__.py`):

```python
# app/api/__init__.py or app/main.py
from app.api.top_hub import router as top_hub_router
app.include_router(top_hub_router)
```

---

### 3) Frontend: Top-Hub Signal Panel (React/Next.js)
File: `/opt/axentx/Costinel/components/TopHubPanel.tsx`

```tsx
// components/TopHubPanel.tsx
'use client';
import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { TrendingUp } from 'lucide-react';

interface Signal {
  hub: string;
  score: number;
  source_file: string;
  proposal: string;
  impact_usd: number;
  rationale: string;
  actions: string[];
}

interface PanelData {
  hub: string;
  generated_at?: string;
  top_signals: Signal[];
}

export default function TopHubPanel({ hubName = 'MOC' }: { hubName?: string }) {
  const [data, setData] = useState<PanelData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`/api/top-hub/panel?hub_name=${hubName}&limit=3`)
      .then((r) => r.json())
      .then(setData)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [hubName]);

  if (loading) return <Card><CardContent className="p-6"><div className="animate-pulse">Loading signals...</div></CardContent></Card>;
  if (!data?.top_signals?.length) return null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-lg font-semibold flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-amber-500" />
          Top-Hub Signals — {data.hub}
        </CardTitle>
        {data.generated_at && (
          <Badge variant="outline" className="text-xs">
            Updated {new Date(data.generated_at).toLocaleDateString()}
          </Badge>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {data.top_signals.map((s, i) => (
          <div key={i} className="border rounded-lg p-3 bg-muted/30">
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1">
                <p className="font-medium text-sm">{s.proposal}</p>
                <p className
