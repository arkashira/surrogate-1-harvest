# Costinel / quality

## Final Synthesized Implementation (Best of Both Candidates)

**Goal:** Add a read-only, CDN-backed Top-Hub Signal Panel to the Costinel dashboard in <2 hours.  
**Core Principle:** *Sense + Signal — ไม่ Execute.* Zero runtime API calls; immune to rate limits; strictly additive.

---

### 1. Data Layer (CDN-first, single source of truth)

- **Path:** `public/data/top-hubs.json`  
  (Serves via CDN; no Authorization header; cacheable and tiny <20KB.)
- **Schema (merged, minimal, correct):**
  ```json
  {
    "hub": "MOC",
    "updated": "2026-05-03T02:30:00Z",
    "signals": [
      {
        "id": "savings-plan-dev-2026-05",
        "title": "Shift idle dev workloads to Savings Plans",
        "impact": "High",
        "context": "Detected 38% idle CPU in dev accounts; 12-month RI/Savings Plan coverage would cut run-rate by ~22%.",
        "href": "/proposals/savings-plan-dev-2026-05"
      },
      {
        "id": "eks-rightsize-ng-costinel",
        "title": "Right-size over-provisioned EKS node groups",
        "impact": "Medium",
        "context": "Node group ng-costinel-prod averages 31% CPU; reduce instance count by 2 and enable Fargate for burst.",
        "href": "/proposals/eks-rightsize-ng-costinel"
      },
      {
        "id": "s3-tiering-logs-2026-05",
        "title": "Enable S3 Intelligent-Tiering for cold logs",
        "impact": "Medium",
        "context": "14 TB of >90d logs on Standard; move to Intelligent-Tiering expected to save ~$1.1k/mo.",
        "href": "/proposals/s3-tiering-logs-2026-05"
      }
    ]
  }
  ```
  - **Why this shape:** Combines Candidate 1’s minimal schema with Candidate 2’s `id` for stable keys and `cdnPath` optional for future assets. No `source`/`ts` noise.

- **Refresh script (build-time, optional cron):**
  - Runs on Mac orchestration machine.
  - Uses HF API + `list_repo_tree` on `knowledge-rag/top-hubs/` to pick latest date folder.
  - Outputs `public/data/top-hubs.json`.
  - **Cron hygiene (non-negotiable):**
    ```bash
    #!/usr/bin/env bash
    # update-top-hub.sh
    set -euo pipefail
    # ...logic to fetch latest top-hub and write public/data/top-hubs.json...
    ```
    - `chmod +x update-top-hub.sh`
    - Crontab: `SHELL=/bin/bash` and invoke via `bash /path/update-top-hub.sh "$@"`

---

### 2. Component (React/TS, zero-runtime-API, graceful fallback)

`src/components/SignalPanel.tsx`
```tsx
import { useEffect, useState } from "react";

type Signal = {
  id: string;
  title: string;
  impact: "High" | "Medium" | "Low";
  context: string;
  href?: string;
};

type TopHubData = {
  hub: string;
  updated: string;
  signals: Signal[];
};

export default function SignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch("/data/top-hubs.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to fetch top-hub signals");
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch(() => {
        setError(true);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="signal-panel card" aria-busy="true">
        <div className="card-body">Loading signals…</div>
      </div>
    );
  }

  if (error || !data || !data.signals?.length) {
    // Read-only, non-breaking: fail silently
    return null;
  }

  const impactColor = (imp: string) => {
    switch (imp) {
      case "High":
        return "text-red-600";
      case "Medium":
        return "text-orange-600";
      default:
        return "text-gray-600";
    }
  };

  return (
    <div className="signal-panel card mb-4" aria-label="Top-hub signals">
      <div className="card-header d-flex justify-content-between align-items-center">
        <strong>Top Hub: {data.hub}</strong>
        <small className="text-muted">Updated {new Date(data.updated).toLocaleDateString()}</small>
      </div>
      <div className="card-body p-0">
        <ul className="list-group list-group-flush">
          {data.signals.slice(0, 3).map((s) => (
            <li key={s.id} className="list-group-item p-3">
              <div className="d-flex justify-content-between align-items-start">
                <div className="flex-1 me-3">
                  <div className="d-flex align-items-center gap-2 mb-1">
                    <span className={`badge ${impactColor(s.impact)}`}>{s.impact}</span>
                    <span className="text-muted small">Signal</span>
                  </div>
                  <h6 className="mb-1">{s.title}</h6>
                  <p className="mb-0 small text-muted">{s.context}</p>
                </div>
                {s.href && (
                  <a href={s.href} className="stretched-link" aria-label={`Details: ${s.title}`} />
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
```

---

### 3. Mount (dashboard layout)

`src/pages/Dashboard.tsx`
```tsx
import SignalPanel from "../components/SignalPanel";

export default function Dashboard() {
  return (
    <div className="container-fluid py-3">
      <SignalPanel />
      {/* rest of dashboard */}
    </div>
  );
}
```

---

### 4. Acceptance Criteria (merged, stricter)

- Panel appears on dashboard showing “Top Hub: MOC” and up to 3 signals.
- Data is loaded from CDN path `/data/top-hubs.json` with **no Authorization header** and **no runtime API calls** (immune to HF 429).
- No runtime errors if CDN fetch fails (graceful fallback: panel hidden, no crash).
- No changes to existing behavior; strictly additive and read-only.
- Build/refresh script (if used) follows cron/script hygiene: shebang, `chmod +x`, `SHELL=/bin/bash`, invoked via `bash script.sh "$@"`.

---

**Result:** One clean, CDN-backed, zero-runtime-API panel that surfaces high-impact hub signals immediately, fits Costinel philosophy, and ships in <2 hours.
