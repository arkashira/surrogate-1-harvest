# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `TOP_HUB_DEFAULT=MOC`).
- Shows the **top 3 actionable, cost-impact proposals** from the knowledge-RAG graph.
- **Zero HF API calls at runtime**: all data served via HF CDN (`huggingface.co/datasets/.../resolve/main/...`).
- Graceful degradation if CDN fails or no proposals exist.

---

### Architecture (fits existing Costinel stack)

| Layer | Responsibility | Key detail |
|------|---------------|------------|
| **Offline build** | `scripts/build-top-hub-cache.py` runs post-RAG, writes `top-hub-cache/{hub_id}/{YYYY-MM-DD}.json` and uploads to `axentx/costinel-top-hub-cache` | Produces one dated JSON per hub; contains exactly 3 proposals |
| **Backend** | FastAPI endpoint `/api/top-hub-signals` | Reads CDN via `requests` (5s timeout), falls back to local `cache/...` in dev; returns `{ hub_id, generated_at, proposals: [{id, title, impact_score, action, context, source_file}] }` |
| **Frontend** | `TopHubSignalPanel` React component mounted in dashboard | Polls endpoint every 60s, non-blocking, dismissible, skeleton + error + empty states |
| **Runtime contract** | No HF API or heavy compute during serving | Only CDN GETs; parquet projection and ranking happen offline |

---

### File changes (incremental)

1. `backend/main.py` — add `/api/top-hub-signals` endpoint.
2. `frontend/src/components/dashboard/TopHubSignalPanel.tsx` — new component.
3. `frontend/src/pages/Dashboard.tsx` — mount panel (top bar or right rail).
4. `scripts/build-top-hub-cache.py` — generate dated hub caches and push to HF dataset repo.
5. `docker-compose.yml` — add env: `TOP_HUB_DEFAULT=MOC`.
6. `requirements.txt` — ensure `requests` present.

---

### Code (merged + hardened)

#### 1. Backend endpoint (`backend/main.py`)
```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from pathlib import Path
import json
import os
from typing import List, Optional
import requests
from pydantic import BaseModel

router = APIRouter()

TOP_HUB_DEFAULT = os.getenv("TOP_HUB_DEFAULT", "MOC")
CACHE_REPO = os.getenv("TOP_HUB_CACHE_REPO", "axentx/costinel-top-hub-cache")
CDN_BASE = f"https://huggingface.co/datasets/{CACHE_REPO}/resolve/main"
LOCAL_CACHE_ROOT = Path("cache")

class ProposalItem(BaseModel):
    id: str
    title: str
    impact_score: float
    action: str
    context: str
    source_file: str

class TopHubSignalsResponse(BaseModel):
    hub_id: str
    generated_at: str
    proposals: List[ProposalItem]

def _load_local_fallback(cache_path: Path) -> Optional[dict]:
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            return None
    return None

@router.get("/api/top-hub-signals", response_model=TopHubSignalsResponse)
async def get_top_hub_signals(hub: Optional[str] = None):
    target_hub = hub or TOP_HUB_DEFAULT
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_rel = f"top-hub-cache/{target_hub}/{today}.json"
    cdn_url = f"{CDN_BASE}/{cache_rel}"

    # Try CDN first (runtime path)
    try:
        resp = requests.get(cdn_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # Normalize expected shape
        if "proposals" not in data:
            raise ValueError("Invalid cache format: missing proposals")
        return TopHubSignalsResponse(hub_id=target_hub, **data)
    except Exception as exc:
        # Dev/local fallback
        local_path = LOCAL_CACHE_ROOT / cache_rel
        local_data = _load_local_fallback(local_path)
        if local_data is not None:
            return TopHubSignalsResponse(hub_id=target_hub, **local_data)

        raise HTTPException(
            status_code=503,
            detail=f"Top-hub signals unavailable for '{target_hub}': {exc}"
        )
```

#### 2. Frontend panel (`frontend/src/components/dashboard/TopHubSignalPanel.tsx`)
```tsx
import React, { useEffect, useState, useCallback } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ExternalLink, TrendingUp, AlertCircle, X } from "lucide-react";

interface Proposal {
  id: string;
  title: string;
  impact_score: number;
  action: string;
  context: string;
  source_file: string;
}

interface TopHubSignals {
  hub_id: string;
  generated_at: string;
  proposals: Proposal[];
}

const TopHubSignalPanel: React.FC<{ hubId?: string }> = ({ hubId }) => {
  const [data, setData] = useState<TopHubSignals | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  const fetchProposals = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (hubId) params.append("hub", hubId);
      const res = await fetch(`/api/top-hub-signals?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err: any) {
      setError(err.message || "Failed to load signals");
    } finally {
      setLoading(false);
    }
  }, [hubId]);

  useEffect(() => {
    fetchProposals();
    const interval = setInterval(fetchProposals, 60000);
    return () => clearInterval(interval);
  }, [fetchProposals]);

  if (dismissed) return null;

  const impactColor = (score: number) => {
    if (score >= 80) return "text-red-700 bg-red-100";
    if (score >= 50) return "text-amber-700 bg-amber-100";
    return "text-emerald-700 bg-emerald-100";
  };

  if (loading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-sm font-medium">
            <span className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4" />
              Top-Hub Signals ({hubId || "MOC"})
            </span>
            <Button variant="ghost" size="icon-xs" onClick={() => setDismissed(true)}>
              <X className="h-3 w-3" />
            </Button>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            <div className="h-4 w-3/4 bg-muted rounded animate-pulse" />
            <div className="h-4 w-1/2 bg-muted rounded animate-pulse" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-sm font-medium">
            <span className="flex items-center gap-2">
              <AlertCircle className="h-4
