# Costinel / quality

## Implementation Plan — Costinel Quality Increment

**Highest-value <2h improvement:**  
Harden the new `GET /api/v1/cost-anomaly/signal` endpoint with deterministic, observable, and testable behavior plus minimal operational quality fixes (logging, error boundaries, schema validation, idempotent 30-day baseline warm-up). This prevents silent failures and makes the signal trustworthy for governance workflows.

**Scope (ship in <2h):**
- Add input validation and safe defaults for date/tenant/service filters.
- Ensure 30-day baseline is computed idempotently (graceful fallback when <30 days exist).
- Add structured logging + request trace ID for auditability.
- Add error boundary (HTTP 4xx/5xx with machine-readable codes).
- Add lightweight unit test for the signal shape and z-score logic.
- Expose a health-checkable readiness probe (`GET /api/v1/health/live`, `/ready`).

**Files to touch (assumed layout from Costinel backend):**
- `backend/src/routes/costAnomaly.ts` (or similar) — endpoint logic
- `backend/src/services/costAnomalyService.ts` — core signal computation
- `backend/src/middleware/requestLogger.ts` (or add inline) — trace/logging
- `backend/src/routes/health.ts` — liveness/readiness
- `backend/tests/costAnomaly.test.ts` — unit tests
- `backend/src/types/costAnomaly.ts` — request/response types

---

### 1) Types (strict, minimal)

```ts
// backend/src/types/costAnomaly.ts
export interface CostAnomalySignalRequest {
  tenantId: string;
  // optional filters; defaults to today in account tz
  date?: string; // YYYY-MM-DD
  service?: string;
  timezone?: string; // e.g. UTC, America/New_York
}

export interface CostAnomalySignalResponse {
  signalId: string; // deterministic hash for idempotency
  tenantId: string;
  date: string; // YYYY-MM-DD
  service: string;
  observedCost: number;
  baselineMean: number;
  baselineStd: number;
  zScore: number;
  severity: 'low' | 'medium' | 'high' | 'critical';
  direction: 'up' | 'down';
  confidence: number; // 0-1 (based on baseline sample size/stability)
  timestamp: string; // ISO when signal generated
  metadata: {
    baselineDaysUsed: number;
    currency: string;
    region?: string;
    accountId?: string;
  };
}

export interface ErrorResponse {
  code:
    | 'VALIDATION_ERROR'
    | 'BASELINE_INSUFFICIENT'
    | 'NOT_FOUND'
    | 'INTERNAL_ERROR';
  message: string;
  details?: unknown;
  requestId?: string;
}
```

---

### 2) Core service — deterministic, safe baseline

```ts
// backend/src/services/costAnomalyService.ts
import { CostAnomalySignalRequest, CostAnomalySignalResponse } from '../types/costAnomaly';

const SEVERITY_THRESHOLDS = [2, 3, 5]; // >5 critical, >3 high, >2 medium

function stdev(values: number[]): number {
  if (values.length < 2) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const sq = values.map((v) => (v - mean) ** 2);
  return Math.sqrt(sq.reduce((a, b) => a + b, 0) / (values.length - 1));
}

function clampZ(z: number): number {
  // cap extreme outliers to avoid explosive signals
  const cap = 10;
  return Math.max(-cap, Math.min(cap, z));
}

function severityFromZ(z: number): CostAnomalySignalResponse['severity'] {
  const az = Math.abs(z);
  if (az >= SEVERITY_THRESHOLDS[2]) return 'critical';
  if (az >= SEVERITY_THRESHOLDS[1]) return 'high';
  if (az >= SEVERITY_THRESHOLDS[0]) return 'medium';
  return 'low';
}

export async function computeSignal(
  req: CostAnomalySignalRequest,
  repo: {
    // repository interface to fetch daily costs
    dailyCostsByService(args: {
      tenantId: string;
      start: string;
      end: string;
      service?: string;
    }): Promise<Array<{ date: string; service: string; cost: number; region?: string; accountId?: string }>>;
  }
): Promise<CostAnomalySignalResponse> {
  const { tenantId, date: targetDateStr, service: targetService, timezone = 'UTC' } = req;
  const targetDate = new Date(targetDateStr || new Date().toISOString().split('T')[0]);
  const targetDateStrNorm = targetDate.toISOString().split('T')[0];

  // 30-day baseline window (exclusive of target day)
  const baselineEnd = new Date(targetDate);
  const baselineStart = new Date(targetDate);
  baselineStart.setDate(baselineStart.getDate() - 30); // 30 days prior

  const baseline = await repo.dailyCostsByService({
    tenantId,
    start: baselineStart.toISOString().split('T')[0],
    end: baselineEnd.toISOString().split('T')[0],
    service: targetService,
  });

  // Group by service (if targetService omitted, pick highest-cost service today)
  const todayCosts = await repo.dailyCostsByService({
    tenantId,
    start: targetDateStrNorm,
    end: targetDateStrNorm,
    service: targetService,
  });

  // If no targetService, pick service with highest cost today
  const candidates = targetService
    ? todayCosts.filter((t) => t.service === targetService)
    : todayCosts;

  if (candidates.length === 0) {
    const err: any = new Error('No cost data for target date/service');
    err.code = 'NOT_FOUND';
    throw err;
  }

  // Pick highest-cost candidate as primary signal
  const primary = candidates.reduce((a, b) => (b.cost > a.cost ? b : a));

  // Build per-service baseline series (if targetService provided, filter)
  const baselineSeries = baseline.filter((b) => b.service === primary.service).map((b) => b.cost);
  const baselineDaysUsed = baselineSeries.length;

  // Graceful fallback when insufficient baseline
  let baselineMean = primary.cost;
  let baselineStd = 0;
  let z = 0;
  let confidence = baselineDaysUsed >= 7 ? Math.min(1, baselineDaysUsed / 30) : 0.5;

  if (baselineDaysUsed >= 2) {
    baselineMean = baselineSeries.reduce((a, b) => a + b, 0) / baselineDaysUsed;
    baselineStd = stdev(baselineSeries);
    if (baselineStd < 0.01 * baselineMean) baselineStd = 0.01 * baselineMean; // avoid divide-by-zero / tiny noise
    z = clampZ((primary.cost - baselineMean) / baselineStd);
    confidence = Math.min(1, baselineDaysUsed / 30);
  } else if (baselineDaysUsed === 1) {
    baselineMean = baselineSeries[0];
    baselineStd = 0.01 * baselineMean;
    z = clampZ((primary.cost - baselineMean) / baselineStd);
    confidence = 0.33;
  } else {
    // No baseline: neutral signal, but still surface today's cost
    baselineMean = primary.cost;
    baselineStd = 0;
    z = 0;
    confidence = 0.2;
  }

  // Deterministic signal id (tenant+date+service hash)
  const crypto = await import('crypto');
  const signalId = crypto
    .createHash('sha256')
    .update(`${tenantId}:${targetDateStrNorm}:${primary.service}`)
    .digest('hex')
    .slice(0, 16);

  return {
    signalId,
    tenantId,
    date: targetDateStrNorm,
    service: primary.service,
    observedCost: primary.cost,
    baselineMean,
    baselineStd,

