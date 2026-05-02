# Costinel / frontend

**Final Consolidated Implementation (Backend + Frontend)**

Scope: Highest-value, read-only, <1h backend + <2h frontend  
Principle: “Sense + Signal — ไม่ Execute” (no self-execution)  
Endpoint: `GET /api/v1/cost-anomaly/signal/top-hub`  

---

### 1) Backend (Python/Flask) — production-ready

**Key decisions**
- Use `knowledge-rag.query_top_hub()` (single call) for correctness and simplicity.  
- Require authentication (JWT or API key) and enforce read-only permissions.  
- Return stable JSON contract for frontend.  
- Fast fail + structured logging; no side effects.

**Implementation**

```python
# app/routes/signals.py
from flask import Blueprint, jsonify, current_app, request, abort
from knowledge_rag import KnowledgeRag

bp = Blueprint("signals", __name__, url_prefix="/api/v1/cost-anomaly/signal")

def _require_auth():
    # Adapt to your auth scheme (Bearer JWT, API key header, etc.)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not _is_valid_token(auth.split(" ")[1]):
        abort(401, "Unauthorized")

def _is_valid_token(token: str) -> bool:
    # Replace with real validation (e.g., jwt.decode or API-key lookup)
    return bool(token and token != "invalid")

@bp.route("/top-hub", methods=["GET"])
def get_top_hub_signal():
    _require_auth()
    try:
        kr = KnowledgeRag()
        payload = kr.query_top_hub()
        # Enforce minimal contract expected by frontend
        if not payload or "hub" not in payload or "score" not in payload:
            current_app.logger.warning("Invalid top-hub payload from knowledge-rag")
            abort(502, "Invalid upstream signal")
        return jsonify(payload)
    except Exception as exc:
        current_app.logger.exception("Top-hub signal failed")
        abort(502, f"Upstream unavailable: {exc}")
```

**Register blueprint** (in app factory)

```python
# app/__init__.py
def create_app():
    app = Flask(__name__)
    from app.routes.signals import bp as signals_bp
    app.register_blueprint(signals_bp)
    return app
```

**API contract (JSON response)**

```json
{
  "hub": "MOC",
  "score": 0.87,
  "context": "Highest connectivity and cost anomaly likelihood this period.",
  "lastUpdated": "2025-06-25T14:32:00Z"
}
```

**Deployment checklist (<1h)**
- Add route + auth check + error handling.  
- Wire `KnowledgeRag().query_top_hub()` (already available per candidates).  
- Add unit test for 200, 401, 502 paths.  
- Deploy behind existing auth gateway; monitor logs for 5xx.

**Commit message**
```
feat: Implement Costinel Top-Hub Signal (Backend) API Endpoint
```

---

### 2) Frontend (TypeScript/React) — production-ready

**Key decisions**
- Expose typed `TopHubSignal` component and `useTopHubSignal` hook.  
- Fail silently (no toasts) for read-only signal; skeleton while loading.  
- Poll every 60s while tab visible; respect stale-time to avoid flicker.  
- Mount near cost-summary/anomaly panel for visibility.

**Files**

`src/lib/api/signals.ts`

```ts
import { useQuery } from '@tanstack/react-query';

export interface TopHubSignal {
  hub: string;
  score: number;
  context: string;
  lastUpdated: string; // ISO
}

async function fetchTopHubSignal(): Promise<TopHubSignal> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });

  if (!res.ok) {
    throw new Error(`Failed to fetch top-hub signal: ${res.status}`);
  }
  return res.json();
}

export function useTopHubSignal(options = {}) {
  return useQuery<TopHubSignal>({
    queryKey: ['top-hub-signal'],
    queryFn: fetchTopHubSignal,
    refetchInterval: 60_000,
    staleTime: 55_000,
    retry: 2,
    ...options,
  });
}
```

`src/components/TopHubSignal.tsx`

```tsx
import React from 'react';
import { useTopHubSignal } from '@/lib/api/signals';

export function TopHubSignal() {
  const { data, isLoading, error } = useTopHubSignal();

  if (isLoading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        <div className="mt-2 h-4 w-24 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  if (error || !data) {
    return null; // Fail silently per read-only signal principle
  }

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Top Hub Signal
          </p>
          <p className="mt-1 text-xl font-semibold">{data.hub}</p>
          <p className="mt-1 text-sm text-muted-foreground">{data.context}</p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-bold">{data.score.toFixed(2)}</p>
          <p className="text-xs text-muted-foreground">score</p>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Updated {new Date(data.lastUpdated).toLocaleString()}
      </p>
    </div>
  );
}
```

Mount in dashboard (`src/pages/Dashboard.tsx`)

```tsx
import { TopHubSignal } from '@/components/TopHubSignal';

export default function Dashboard() {
  return (
    <div className="grid gap-6">
      {/* Existing cost summary / anomaly panels */}
      <div className="grid gap-4 md:grid-cols-3">
        <div className="md:col-span-2">
          {/* existing panels */}
        </div>
        <div>
          <TopHubSignal />
        </div>
      </div>
    </div>
  );
}
```

---

### 3) Quick validation & rollout

Backend
- `GET /api/v1/cost-anomaly/signal/top-hub` returns 200 + contract above when authenticated.  
- 401 on missing/invalid auth.  
- 502 if knowledge-rag fails.

Frontend
- Card appears in dashboard with hub, score, context, timestamp.  
- Skeleton → data → silent fail (no toasts).  
- Auto-refresh every 60s.

Security & ops
- Auth enforced at route.  
- No write endpoints; read-only.  
- Monitor error rates and latency; alert on 5xx spikes.

This merges the strongest backend clarity (Candidate 2’s `query_top_hub` + Candidate 1’s structure) with the strongest frontend completeness (Candidate 3), while enforcing correctness, security, and silent failure for a non-invasive read-only signal.
