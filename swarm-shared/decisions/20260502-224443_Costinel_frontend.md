# Costinel / frontend

## Final Decision  
**Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint** that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context.  
- No writes, no training, no side effects.  
- Bypasses HF API during data fetch by using a pre-listed file manifest and HF CDN URLs (`resolve/main/...`).  
- Reuses existing RAG/graph layer and can ship in **≤2h** as a pure read path.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Concrete deliverable |
|------|-------|------|----------------------|
| 1 | BE | 10m | Confirm project structure and pick runtime (FastAPI/Express/SvelteKit). |
| 2 | BE | 25m | Implement `top_hub(date)` in knowledge-rag layer: deterministic graph query returning `{ hub, score, entities }`. |
| 3 | BE | 20m | Implement `strongest_signal_for_hub(hub, date, manifest, cdn_fetch)` returning `{ entity, metric, severity, delta, window, related_docs, source_file }`. |
| 4 | BE | 15m | Add HF CDN-safe loader: `load_manifest()` + `cdn_fetch()` using `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth). |
| 5 | BE | 15m | Create `GET /api/v1/cost-anomaly/signal/top-hub` route with UTC date, caching (`Cache-Control: public, max-age=60`), and structured error handling. |
| 6 | FE | 30m | Add dashboard widget: “Top-hub anomaly” card showing hub, severity, metric, delta, and quick links to related docs; loading/error states; auto-refresh 60s. |
| 7 | QA | 15m | Smoke test: endpoint deterministic, no writes, CDN fetch works, widget renders and updates. |

Total: **~2h**.

---

## API Endpoint (FastAPI example)

```python
# app/api/v1/cost_anomaly.py
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from services.knowledge_rag import top_hub, strongest_signal_for_hub
from services.hf_cdn import load_manifest, cdn_fetch

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub")
async def get_top_hub_signal():
    try:
        today = datetime.now(timezone.utc).date()
        hub_info = top_hub(date=today)
        if not hub_info:
            return {"ok": True, "data": None, "message": "No top hub found"}

        manifest = load_manifest("batches/mirror-merged/file_manifest.json")
        signal = strongest_signal_for_hub(
            hub=hub_info["hub"],
            date=today,
            manifest=manifest,
            cdn_fetch=cdn_fetch,
        )
        return {
            "ok": True,
            "data": {
                "hub": hub_info["hub"],
                "hub_score": hub_info["score"],
                "signal": signal,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

---

## Knowledge-RAG Layer

```python
# services/knowledge_rag.py
from datetime import date
from typing import Optional, Dict, Any

def top_hub(date: date) -> Optional[Dict[str, Any]]:
    """
    Deterministic graph query for the most-connected hub on `date`.
    Returns: {"hub": str, "score": float, "entities": [str]}
    """
    result = graph.query(
        """
        MATCH (h:Hub)-[r:CONNECTED_TO]->(e:Entity)
        WHERE date(r.ts) = $date
        RETURN h.name AS hub, sum(r.weight) AS score, collect(distinct e.name) AS entities
        ORDER BY score DESC
        LIMIT 1
        """,
        date=date.isoformat(),
    )
    if not result:
        return None
    row = result[0]
    return {"hub": row["hub"], "score": row["score"], "entities": row["entities"]}


def strongest_signal_for_hub(
    hub: str,
    date: date,
    manifest: Dict[str, Any],
    cdn_fetch,
) -> Dict[str, Any]:
    """
    Deterministic selection of strongest cost-anomaly signal for `hub` on `date`.
    Uses manifest + CDN fetch to avoid HF API.
    """
    date_str = date.isoformat()
    files = manifest.get(date_str, [])
    best = None

    for fpath in files:
        url = f"https://huggingface.co/datasets/Costinel/mirror/resolve/main/{fpath}"
        records = cdn_fetch(url, project_to=["entity", "metric", "severity", "delta", "window", "related_docs"])
        for r in records:
            if r.get("entity") and hub.lower() in r["entity"].lower():
                if best is None or r["severity"] > best["severity"]:
                    best = {
                        "entity": r["entity"],
                        "metric": r["metric"],
                        "severity": r["severity"],
                        "delta": r["delta"],
                        "window": r["window"],
                        "related_docs": r.get("related_docs", []),
                        "source_file": fpath,
                        "found": True,
                    }
    if best is None:
        return {"found": False, "reason": "No anomaly signals for hub today"}
    return best
```

---

## HF CDN Helpers

```python
# services/hf_cdn.py
import json
import requests
from typing import Any, Dict, List

def load_manifest(manifest_path: str) -> Dict[str, List[str]]:
    with open(manifest_path) as f:
        return json.load(f)

def cdn_fetch(url: str, project_to: List[str]) -> List[Dict[str, Any]]:
    """
    Fetch newline-delimited JSON via HF CDN (resolve/main). No Authorization header.
    """
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    rows = []
    for line in resp.text.strip().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        rows.append({k: obj.get(k) for k in project_to})
    return rows
```

---

## Frontend Widget (React)

```tsx
// components/TopHubAnomalyCard.tsx
import useSWR from "swr";

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubAnomalyCard() {
  const { data, error } = useSWR("/api/v1/cost-anomaly/signal/top-hub", fetcher, {
    refreshInterval: 60_000,
  });

  if (error) return <div className="alert alert-error">Failed to load top-hub signal.</div>;
  if (!data) return <div className="skeleton h-32 w-full"></div>;
  if (!data.data) return <div className="alert">No top-hub anomaly today.</div>;

  const { hub, hub_score, signal } = data.data;
  return (
    <div className="card">
      <h3 className="card-title">Top-Hub Anomaly</h3>
      <p><strong>{hub}</strong> (score: {hub_score?.toFixed(2)})</p>
      {signal.found ? (
        <>
          <p><strong>{signal.entity}</strong> — {signal.metric}</p>
          <p>Severity: {signal.severity} | Delta: {signal.delta}</p>
          <p className="text-sm text-base-content/60">Window: {signal.window}</p>
          {signal.related_docs?.length > 0 && (
            <div className="mt-2">
              <span className="text-sm font-semibold">Docs: </span>
              {signal.related_docs.map((d: string) => (
                <a key={d} href={`/docs/${d}`} className="link link-sm
