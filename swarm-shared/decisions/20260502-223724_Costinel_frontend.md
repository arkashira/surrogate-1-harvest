# Costinel / frontend

**Final Synthesized Implementation**  
*(Best of Candidate 1 + Candidate 2, with contradictions resolved for correctness and concrete actionability)*

---

## Highest-Value Incremental Improvement (<2h)

**Improvement:**  
Add a deterministic, read-only “Today’s strongest cost-anomaly signal” widget to the Costinel frontend that calls `GET /api/v1/cost-anomaly/signal` and renders a single actionable card (service, delta, severity, description, recommendation). Includes optimistic loading, empty, and error states; a manual refresh button; and accessibility best practices.

**Why this ships fast and adds value:**
- One new frontend component + one API integration (no backend changes).
- Immediately surfaces the most actionable cost signal (aligns with “Sense + Signal”).
- Deterministic contract and states let QA and tests lock behavior quickly.
- Fits existing design tokens and dashboard layout.

---

## Implementation Plan

1. **Define API contract (frontend expectation)**  
   Expect `GET /api/v1/cost-anomaly/signal` to return:
   ```json
   {
     "service": "string",
     "delta": "+12.5%",
     "severity": "low|medium|high|critical",
     "description": "string",
     "recommendation": "string",
     "timestamp": "2026-05-02T22:30:00Z"
   }
   ```
   - No signal: `204 No Content`.  
   - Errors: `4xx/5xx` with optional JSON `{ error: "string" }`.

2. **Create component: `TodayAnomalySignal`**  
   - Location: `src/components/dashboard/TodayAnomalySignal.tsx`  
   - Use `fetch` with `AbortController` and 10s timeout.  
   - States: `idle` → `loading` → `success` / `empty` / `error`.  
   - Auto-fetch on mount; manual refresh button.  
   - Map `severity` to color/icon (reuse existing tokens).

3. **Wire into dashboard**  
   - Import into main dashboard layout (e.g., `src/pages/Dashboard.tsx`).  
   - Place near the top where “Sense + Signal” is emphasized.

4. **Add minimal tests**  
   - One unit test for render states (success/empty/error).  
   - One integration smoke test that mocks fetch and verifies DOM.

5. **Polish & accessibility**  
   - Semantic markup (`<article>`, `aria-live="polite"` for updates).  
   - Keyboard-accessible refresh button.  
   - Respect `prefers-reduced-motion`.

6. **Build & verify**  
   - Run dev server, verify card appears and API call fires.  
   - Verify graceful handling of 204 and network errors.

**Estimated effort:** ~90–110 minutes.

---

## Code Snippets

### Component: `src/components/dashboard/TodayAnomalySignal.tsx`
```tsx
import { useEffect, useState, useCallback } from "react";
import "./TodayAnomalySignal.css";

type Signal = {
  service: string;
  delta: string;
  severity: "low" | "medium" | "high" | "critical";
  description: string;
  recommendation: string;
  timestamp: string;
};

type FetchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: Signal }
  | { status: "empty" }
  | { status: "error"; message: string };

const SEVERITY_ICON = {
  low: "🔍",
  medium: "⚠️",
  high: "🚨",
  critical: "🔥",
} as const;

const SEVERITY_COLOR = {
  low: "var(--color-info)",
  medium: "var(--color-warning)",
  high: "var(--color-alert)",
  critical: "var(--color-critical)",
} as const;

export default function TodayAnomalySignal() {
  const [state, setState] = useState<FetchState>({ status: "idle" });

  const fetchSignal = useCallback(async (signal?: AbortSignal) => {
    setState({ status: "loading" });
    try {
      const res = await fetch("/api/v1/cost-anomaly/signal", {
        method: "GET",
        headers: { Accept: "application/json" },
        signal,
      });

      if (res.status === 204) {
        setState({ status: "empty" });
        return;
      }

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }

      const data = (await res.json()) as Signal;
      setState({ status: "success", data });
    } catch (err: any) {
      if (err.name === "AbortError") return;
      setState({ status: "error", message: err.message || "Unknown error" });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);
    fetchSignal(controller.signal);
    return () => {
      clearTimeout(timeout);
      controller.abort();
    };
  }, [fetchSignal]);

  const handleRefresh = () => fetchSignal();

  if (state.status === "idle" || state.status === "loading") {
    return (
      <aside className="anomaly-signal loading" aria-busy="true">
        <div className="anomaly-signal__skeleton" />
      </aside>
    );
  }

  if (state.status === "empty") {
    return (
      <aside className="anomaly-signal empty" aria-live="polite">
        <p>No strong cost anomaly detected today.</p>
        <button onClick={handleRefresh} className="anomaly-signal__refresh">
          Refresh
        </button>
      </aside>
    );
  }

  if (state.status === "error") {
    return (
      <aside className="anomaly-signal error" role="alert" aria-live="assertive">
        <p>Error loading signal: {state.message}</p>
        <button onClick={handleRefresh} className="anomaly-signal__refresh">
          Retry
        </button>
      </aside>
    );
  }

  const { data } = state;
  const icon = SEVERITY_ICON[data.severity];
  const color = SEVERITY_COLOR[data.severity];

  return (
    <article
      className="anomaly-signal"
      aria-live="polite"
      style={{ borderColor: color }}
    >
      <header className="anomaly-signal__header">
        <span className="anomaly-signal__icon" style={{ color }}>
          {icon}
        </span>
        <h2 className="anomaly-signal__title">Today’s Strongest Cost-Anomaly Signal</h2>
        <button
          onClick={handleRefresh}
          className="anomaly-signal__refresh"
          aria-label="Refresh signal"
        >
          ↻
        </button>
      </header>

      <div className="anomaly-signal__body">
        <p className="anomaly-signal__service">
          <strong>Service:</strong> {data.service}
        </p>
        <p className="anomaly-signal__delta">
          <strong>Delta:</strong> {data.delta}
        </p>
        <p className="anomaly-signal__description">{data.description}</p>
        <p className="anomaly-signal__recommendation">
          <strong>Recommendation:</strong> {data.recommendation}
        </p>
      </div>

      <footer className="anomaly-signal__footer">
        <small>
          Updated: {new Date(data.timestamp).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}
        </small>
      </footer>
    </article>
  );
}
```

### Example API Response
```json
{
  "service": "AWS EC2",
  "delta": "+12.5%",
  "severity": "high",
  "description": "Unexpected spike in instance usage across us-east
