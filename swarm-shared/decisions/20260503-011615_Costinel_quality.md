# Costinel / quality

## Final Implementation Plan — `/api/v1/sense/top-hub-signal`

**Estimated effort:** <2h  
**Scope:** Single read-only endpoint that senses top-hub signals and returns actionable proposals. Strictly follows Costinel philosophy: **Sense + Signal — ไม่ Execute**.

---

### Architecture (Costinel-aligned)
- **Sense** — query knowledge-rag (or cached graph) for the most-connected hub (e.g., "MOC") and related artifacts.
- **Signal** — return enriched insights + prioritized proposals with full auditability.
- **No execution** — zero state mutation; proposals require human review/approval.
- **Auditability** — every response includes `proposal_id`, `trace_id`, `hub_context`, and provenance.

---

### Implementation Steps

1. Add endpoint `GET /api/v1/sense/top-hub-signal`
   - Query knowledge-rag (reuse `#knowledge-rag #graph #hub` pattern) with short timeout.
   - Return shape: `{ hub, score, insights[], relatedDocs[], proposals[], audit }`.
   - Graceful degradation if hub not found (return 200 with empty/default payload).

2. Integrate knowledge-rag CLI safely (Bash best practices)
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`.
   - Invoke via `bash <script> "$@"`; set `SHELL=/bin/bash` in cron/env if scheduled.
   - Validate JSON output; fail fast with clear errors.

3. Add lightweight frontend widget (optional, display-only)
   - Fetch endpoint and render top-hub card with insights and proposals.
   - No mutations; links open docs or proposal details.

4. Add tests (smoke + contract)
   - Endpoint returns 200 + expected shape.
   - Handles missing hub/script failures gracefully (502 on upstream failure, 200 on empty).

---

### Code Snippets

#### 1) Backend endpoint (FastAPI)

```python
# File: app/api/v1/endpoints/sense.py
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import subprocess
import json
import os
import uuid
from datetime import datetime, timezone

router = APIRouter()

def query_top_hub_via_rag() -> Dict[str, Any]:
    """
    Uses knowledge-rag CLI to get top hub and related insights.
    Pattern: #knowledge-rag #graph #hub
    """
    script = "/opt/axentx/Costinel/scripts/knowledge_rag_top_hub.sh"
    if not os.path.isfile(script):
        raise RuntimeError("knowledge-rag top-hub script not found")

    result = subprocess.run(
        ["bash", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "SHELL": "/bin/bash"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"knowledge-rag failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from knowledge-rag: {exc}") from exc


@router.get("/top-hub-signal", response_model=Dict[str, Any])
def top_hub_signal() -> Dict[str, Any]:
    """
    Sense top-hub signals and return actionable proposals.
    Costinel philosophy: Sense + Signal — ไม่ Execute
    """
    trace_id = str(uuid.uuid4())
    try:
        hub_data = query_top_hub_via_rag()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    hub = hub_data.get("hub", "unknown")
    score = float(hub_data.get("score", 0.0))

    # Build actionable proposals (human review required)
    proposals = [
        {
            "proposal_id": f"proposal-{hub}-{i}",
            "title": f"Review {hub} governance posture",
            "action": "review",
            "target": "change-management",
            "priority": "high" if score > 0.8 else "medium",
            "reason": (
                "Top hub detected with high connectivity; validate cost governance, "
                "access controls, and change management controls."
            ),
            "next_steps": [
                "Validate cost allocation tags",
                "Review IAM boundaries for shared services",
                "Check recent change approvals"
            ],
            "requires_approval": True,
        }
        for i in range(1, 4)
    ]

    payload = {
        "hub": hub,
        "score": score,
        "insights": hub_data.get("insights", []),
        "relatedDocs": hub_data.get("related_docs", []),
        "proposals": proposals,
        "audit": {
            "proposal_id": f"batch-{trace_id}",
            "trace_id": trace_id,
            "hub_context": {
                "name": hub,
                "score": score,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
            "source": "knowledge-rag",
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "philosophy": "Sense + Signal — ไม่ Execute",
            "execution_allowed": False,
        },
    }
    return payload
```

#### 2) knowledge-rag top-hub wrapper (safe Bash)

```bash
#!/usr/bin/env bash
# File: scripts/knowledge_rag_top_hub.sh
# Pattern: #bash #script-error #knowledge-rag #graph #hub
set -euo pipefail

export SHELL=/bin/bash

# Replace with actual knowledge-rag CLI invocation:
# knowledge-rag top-hub --format json --top-n 1
# Simulated structured output for compatibility:
cat <<'JSON'
{
  "hub": "MOC",
  "score": 0.92,
  "insights": [
    "MOC is the most-connected hub (2026-04-27 pattern).",
    "High cross-account cost correlation detected.",
    "Recommended governance review for shared services."
  ],
  "related_docs": [
    {"id": "doc-001", "title": "MOC Governance Playbook", "url": "/docs/moc-playbook"},
    {"id": "doc-002", "title": "Cost Anomaly Patterns", "url": "/docs/cost-anomalies"}
  ]
}
JSON
```

Make executable:
```bash
chmod +x /opt/axentx/Costinel/scripts/knowledge_rag_top_hub.sh
```

#### 3) Register route (FastAPI)

```python
# File: app/api/v1/api.py
from fastapi import APIRouter
from app.api.v1.endpoints import sense

api_router = APIRouter()
api_router.include_router(sense.router, prefix="/sense", tags=["sense"])
```

#### 4) Optional frontend widget (React)

```tsx
// File: src/components/TopHubSignalCard.tsx
import { useEffect, useState } from "react";

interface Insight {
  text: string;
}
interface RelatedDoc {
  id: string;
  title: string;
  url: string;
}
interface Proposal {
  proposal_id: string;
  title: string;
  action: string;
  reason: string;
  next_steps: string[];
  priority: string;
}

export default function TopHubSignalCard() {
  const [signal, setSignal] = useState<{
    hub: string;
    score: number;
    insights: Insight[];
    relatedDocs: RelatedDoc[];
    proposals: Proposal[];
    audit: { philosophy: string; execution_allowed: boolean };
  } | null>(null);

  useEffect(() => {
    fetch("/api/v1/sense/top-hub-signal")
      .then((r) => r.json())
      .then(setSignal)
      .catch(console.error);
  }, []);

  if (!signal) return <div>Loading top-hub signal...</div>;

  return (
    <div className="p-4 border rounded bg-white shadow">
      <h3 className="font-bold">Top Hub: {signal.hub} (score: {signal.score.toFixed(2)})</h3>
      <p className="text-sm text-gray-500">{
