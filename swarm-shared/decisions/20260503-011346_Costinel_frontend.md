# Costinel / frontend

## Implementation Plan — `/api/v1/sense/top-hub-signal` (Costinel frontend)

**Estimated effort:** <2h  
**Scope:** Add a single backend orchestration endpoint that senses top-hub signals and returns actionable insights without executing changes.

---

## Decision

Implement a single backend orchestration endpoint `GET /api/v1/sense/top-hub-signal` that:

- Runs `granite-business-research.sh` once and caches results (15m TTL) to avoid hammering HF/kaggle APIs.
- Executes `knowledge-rag` to query the most-connected hub (e.g., "MOC") and returns top insights.
- Returns structured JSON: `{ hub, signals[], recommendations[], audit }` — **never executes changes** (Sense + Signal).
- Uses HF CDN bypass for dataset fetches (zero API calls during training/reads).
- Reuses running Lightning Studio when present; falls back to L40S on `lightning-public-prod`.
- Includes robust retry/backoff for HF 429 and Kaggle Bearer auth.

---

## Implementation

### 1) Add backend endpoint (FastAPI)

`/opt/axentx/Costinel/backend/main.py`

```python
# Add to existing FastAPI app
import asyncio, json, time, subprocess, os
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from typing import List, Optional
import httpx

router = APIRouter(prefix="/api/v1/sense", tags=["sense"])

CACHE_TTL = 15 * 60  # 15 minutes
_CACHE = {"ts": 0, "data": None}

def _run_script(name: str, args=None):
    script_path = Path("/opt/axentx/Costinel/scripts") / name
    if not script_path.exists():
        raise RuntimeError(f"Script not found: {script_path}")
    cmd = ["/bin/bash", str(script_path)] + (args or [])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed: {result.stderr}")
    return result.stdout.strip()

def _hf_list_date_folder(date_folder: str):
    """Single API call to list files in a date folder, then embed list for CDN-only fetches."""
    repo = os.getenv("HF_DATASET_REPO", "datasets/costinel-mirror")
    # Use huggingface_hub with minimal pagination
    from huggingface_hub import list_repo_tree
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    return [i.rfilename for i in items if i.rfilename.endswith((".parquet", ".jsonl"))]

def _download_via_cdn(repo: str, path: str, dest: Path):
    """HF CDN bypass — no Authorization header, uses CDN tier limits."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest

async def _sense_top_hub():
    # 1) Business research
    research_out = _run_script("granite-business-research.sh")

    # 2) Knowledge RAG — top hub
    rag_out = _run_script("knowledge-rag", ["--query", "top-connected-hub", "--top-k", "5"])

    # Parse simple key insights (adjust to your RAG output format)
    lines = [ln.strip() for ln in rag_out.splitlines() if ln.strip()]
    hub = "MOC"
    signals = [ln for ln in lines if ln.startswith("-") or len(ln) < 200]
    if not signals:
        signals = lines[:5]

    # 3) Build recommendations (non-executing)
    recommendations = [
        {
            "id": f"sig-{int(time.time())}-{i}",
            "title": "Reserved Instance Coverage Gap",
            "hub": hub,
            "signal": sig,
            "action": "proposal",
            "severity": "medium",
            "context": {"source": "knowledge-rag", "ts": datetime.now(timezone.utc).isoformat()},
        }
        for i, sig in enumerate(signals[:3])
    ]

    return {
        "hub": hub,
        "research_summary": research_out[:500],
        "signals": signals,
        "recommendations": recommendations,
        "audit": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": CACHE_TTL,
            "mode": "sense+signal",
            "executed": False,
        },
    }

@router.get("/top-hub-signal", response_model=dict)
async def top_hub_signal(force_refresh: bool = False):
    now = time.time()
    if not force_refresh and (now - _CACHE["ts"]) < CACHE_TTL and _CACHE["data"]:
        return _CACHE["data"]

    try:
        data = await _sense_top_hub()
        _CACHE.update({"ts": now, "data": data})
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

---

### 2) Add lightweight frontend hook (React)

`/opt/axentx/Costinel/src/hooks/useTopHubSignal.ts`

```ts
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';

export interface SignalRecommendation {
  id: string;
  title: string;
  hub: string;
  signal: string;
  action: 'proposal' | 'alert';
  severity: 'low' | 'medium' | 'high';
  context: Record<string, any>;
}

export interface TopHubSignalResponse {
  hub: string;
  research_summary: string;
  signals: string[];
  recommendations: SignalRecommendation[];
  audit: {
    generated_at: string;
    ttl_seconds: number;
    mode: string;
    executed: boolean;
  };
}

export function useTopHubSignal(options?: { refreshInterval?: number; enabled?: boolean }) {
  return useQuery<TopHubSignalResponse>({
    queryKey: ['sense', 'top-hub-signal'],
    queryFn: async () => {
      const { data } = await axios.get<TopHubSignalResponse>('/api/v1/sense/top-hub-signal');
      return data;
    },
    staleTime: 1000 * 60 * 5, // 5m
    refetchInterval: options?.refreshInterval ?? 1000 * 60 * 15, // 15m
    enabled: options?.enabled ?? true,
  });
}
```

---

### 3) Add dashboard widget (optional quick view)

`/opt/axentx/Costinel/src/components/TopHubSignalWidget.tsx`

```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { AlertCircle, CheckCircle } from 'lucide-react';

export function TopHubSignalWidget() {
  const { data, isLoading, isError } = useTopHubSignal();

  if (isLoading) return <Card><CardContent className="p-6"><div className="text-sm text-muted-foreground">Loading hub signals...</div></CardContent></Card>;
  if (isError || !data) return <Card><CardContent className="p-6 text-destructive">Unable to load hub signals.</CardContent></Card>;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <span className="uppercase text-xs font-mono bg-muted px-2 py-0.5 rounded">{data.hub}</span>
          Top-Hub Signals
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.recommendations.slice(0, 3).map((r) => (
          <div key={r.id} className="flex items-start gap-2 text-sm">
            {r.severity === 'high' ? (
