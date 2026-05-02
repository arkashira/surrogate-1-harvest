# Costinel / backend

## Final Synthesis — Highest-Value, Correct, Actionable Increment (≤2h)

I merged the strongest, non-contradictory parts of both proposals and resolved conflicts in favor of **correctness + concrete actionability**:

- **Correctness**: deterministic, pure functions; idempotent writes; audit-ready metadata; no execution side-effects.  
- **Actionability**: one clearly scoped backend signal pipeline with a read-only API and a CLI/job entrypoint; minimal infra; full test plan.

---

### 1) Goal (one sentence)
Add a **read-only Cost Discovery & Anomaly Signal pipeline** that surfaces deterministic, audit-ready daily AWS cost signals (anomalies) per environment with zero execution side-effects.

---

### 2) Scope (in scope)
- Backend model + service + repository for `CostAnomalySignal`.
- Deterministic anomaly detection helper (pure function, z-score, idempotent).
- Scheduled job (once/day) or CLI command to ingest daily cost export and persist signals.
- Read-only API: `GET /api/v1/signals/cost-anomalies` (filter by env/status) and `GET /api/v1/signals/cost-anomalies/{env}/latest`.
- Minimal audit metadata and deduplication.
- Tests: unit for detection helper; integration for API.

### Out of scope
- Real-time cloud connectors (use local fixture/export).
- Execution/remediation hooks.
- RI coverage or idle-resource signals (can be added next).
- Frontend changes (except a small note in README).

---

### 3) Architecture (clean, minimal)
- **Model**: `CostAnomalySignal` (TypeScript/NestJS or Python/FastAPI — pick stack; snippets below for both).
- **Detection**: pure helper `detectCostAnomalies(history)` returning deterministic results.
- **Repository/Service**: CRUD + dedupe by `(environment, date, service, metric)`.
- **Job/CLI**: `discovery run --env prod --infile ./daily_costs.csv` or scheduled worker.
- **API**: read-only endpoints; reuse existing auth middleware.

---

### 4) Data model (canonical fields)
- `id` (uuid)
- `environment` (string)
- `date` (date, YYYY-MM-DD)
- `service` (string)
- `metric` (string, default `daily_spend_usd`)
- `value` (decimal)
- `baseline_mean`, `baseline_std`, `z_score` (decimal)
- `severity` (`medium` | `high` | `critical`)
- `description` (string)
- `source` (string, e.g. `aws_ce_daily`)
- `status` (`new` | `reviewed` | `resolved`)
- `created_at` (timestamp)
- `audit_meta` (JSONB / dict, e.g. `{detected_by, version, run_id}`)

Deduplication key: `(environment, date, service, metric)`.

---

### 5) Detection helper (pure, deterministic)

#### TypeScript (NestJS style)

```ts
// src/signals/cost-anomaly.helper.ts
export interface DailyPoint {
  date: string; // YYYY-MM-DD
  service: string;
  spend: number;
}

export interface AnomalyResult {
  date: string;
  service: string;
  value: number;
  baseline_mean: number;
  baseline_std: number;
  z_score: number;
  severity: 'medium' | 'high' | 'critical';
  description: string;
}

function stdev(values: number[]): number {
  if (values.length < 2) return 0;
  const mean = values.reduce((s, v) => s + v, 0) / values.length;
  const variance = values.reduce((s, v) => s + (v - mean) ** 2, 0) / (values.length - 1);
  return Math.sqrt(variance);
}

export function detectCostAnomalies(
  history: DailyPoint[],
  window = 30,
  minBaseline = 7,
  zMedium = 2.5,
  zHigh = 3.0,
  zCritical = 4.0
): AnomalyResult[] {
  const byService: Record<string, DailyPoint[]> = {};
  for (const p of history) {
    (byService[p.service] = byService[p.service] || []).push(p);
  }

  const results: AnomalyResult[] = [];

  for (const [service, points] of Object.entries(byService)) {
    if (points.length < 2) continue;
    const sorted = points.sort((a, b) => a.date.localeCompare(b.date));
    const latest = sorted[sorted.length - 1];
    const baselinePoints = sorted.slice(-window - 1, -1); // exclude latest
    if (baselinePoints.length < minBaseline) continue;

    const baselineValues = baselinePoints.map((p) => p.spend);
    const mean = baselineValues.reduce((s, v) => s + v, 0) / baselineValues.length;
    const sd = stdev(baselineValues);
    const z = sd === 0 ? 0 : (latest.spend - mean) / sd;

    let severity: AnomalyResult['severity'] | null = null;
    const absZ = Math.abs(z);
    if (absZ >= zCritical) severity = 'critical';
    else if (absZ >= zHigh) severity = 'high';
    else if (absZ >= zMedium) severity = 'medium';

    if (severity) {
      results.push({
        date: latest.date,
        service,
        value: latest.spend,
        baseline_mean: Number(mean.toFixed(2)),
        baseline_std: Number(sd.toFixed(2)),
        z_score: Number(z.toFixed(2)),
        severity,
        description: `${service} daily spend ${latest.spend.toFixed(
          2
        )} USD (z=${z.toFixed(2)}) vs baseline ${mean.toFixed(2)} ± ${sd.toFixed(2)}`,
      });
    }
  }

  return results;
}
```

#### Python (FastAPI style)

```python
# app/signals/cost_anomaly_helper.py
from __future__ import annotations
from typing import List, Dict, Any
from dataclasses import dataclass
import math

@dataclass
class DailyPoint:
    date: str
    service: str
    spend: float

@dataclass
class AnomalyResult:
    date: str
    service: str
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    severity: str  # medium/high/critical
    description: str

def _stdev(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)

def detect_cost_anomalies(
    history: List[DailyPoint],
    window: int = 30,
    min_baseline: int = 7,
    z_medium: float = 2.5,
    z_high: float = 3.0,
    z_critical: float = 4.0,
) -> List[AnomalyResult]:
    by_service: Dict[str, List[DailyPoint]] = {}
    for p in history:
        by_service.setdefault(p.service, []).append(p)

    results: List[AnomalyResult] = []
    for service, points in by_service.items():
        if len(points) < 2:
            continue
        sorted_pts = sorted(points, key=lambda x: x.date)
        latest = sorted_pts[-1]
        baseline = sorted_pts[-window - 1 : -1]
        if len(baseline) < min_baseline:
            continue

        vals = [p.spend for p in baseline]
        mean = sum(vals) / len(vals)
        sd = _stdev(vals)
        z = 0.0 if sd == 0 else (latest.spend - mean) / sd
        abs_z = abs(z)

        severity = None
        if abs_z >= z_critical:
            severity = "critical"
        elif abs_z >= z_high:
            severity = "high"
        elif abs_z >= z_medium:
           
