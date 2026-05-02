# Costinel / frontend

**Costinel Top-Hub Signal (Backend) — Final Synthesis**  
*Scope: Highest-value, read-only, < 1 hour to ship MVP*  
*Endpoint: `GET /api/v1/cost-anomaly/signal/top-hub`*  
*Principle: “Sense + Signal — ไม่ Execute” (no side effects, no mutations)*

---

### 1. Architecture (minimal, production-ready)
- **API Gateway**: AWS API Gateway (managed auth, throttling, observability).  
- **Service**: Single lightweight service (e.g., FastAPI in AWS Lambda or ECS Fargate) to stay within 1-hour target.  
- **Data store**: Amazon Aurora Serverless (PostgreSQL) or DynamoDB (if extreme scale/simplicity needed).  
- **Ingestion (out of scope for this endpoint)**: Assume cost data is already landed hourly from AWS Cost Explorer, GCP Billing Export, Azure Cost Management into the store.  
- **Separation of concerns**: Keep anomaly detection *read-side* (materialized view or cached result) so the API remains fast and side-effect-free.

---

### 2. Data model (read-optimized)
Table/collection: `hub_cost_metrics` (or view)  
Key fields:
- `hub_id` (string)  
- `window_start` (timestamp)  
- `window_end` (timestamp)  
- `cost` (numeric)  
- `expected_cost` (numeric, nullable)  
- `anomaly_score` (float, nullable)  
- `signal` (jsonb/json, nullable)  

Precompute anomaly scores and signals in a periodic job (e.g., hourly Lambda) so the API is a simple read.

---

### 3. Top-hub signal logic (correct + actionable)
- **Anomaly detection**: Use a robust, explainable method:
  - Primary: Modified Z-score on recent cost deltas (resistant to outliers).  
  - Secondary (optional): Isolation Forest on cost + usage features for multivariate anomalies.  
- **Top-hub selection**: Rank hubs by `anomaly_score * cost` (impact-weighted) over the latest full window. Return top N (e.g., top 5).  
- **Threshold**: Only emit signals where `anomaly_score >= 3.5` (strong evidence) and `cost_delta_pct >= 20%` to avoid noise.

---

### 4. API contract (concrete)
**Request**  
`GET /api/v1/cost-anomaly/signal/top-hub`  
Query params (optional):  
- `window_end` (ISO8601, default: last completed hour)  
- `limit` (int, default: 5, max: 20)  

**Response (200 OK)**  
```json
{
  "window_end": "2025-06-19T22:00:00Z",
  "generated_at": "2025-06-19T23:01:12Z",
  "top_hub_signals": [
    {
      "hub_id": "hub-prod-01",
      "anomaly_type": "cost_spike",
      "cost": 12450.32,
      "expected_cost": 8100.00,
      "cost_delta_pct": 53.7,
      "anomaly_score": 4.21,
      "window_start": "2025-06-19T21:00:00Z",
      "window_end": "2025-06-19T22:00:00Z",
      "severity": "high"
    }
  ]
}
```

**Errors**  
- `400 Bad Request` — invalid params.  
- `429 Too Many Requests` — rate limit.  
- `500 Internal Server Error` — data unavailable (include minimal safe message).

---

### 5. Implementation (FastAPI, < 1 hour MVP)
```python
# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import List, Optional
import os
import asyncpg  # or boto3 for DynamoDB

app = FastAPI()

DB_URL = os.getenv("DB_URL")

class TopHubSignal(BaseModel):
    hub_id: str
    anomaly_type: str
    cost: float
    expected_cost: float
    cost_delta_pct: float
    anomaly_score: float
    window_start: str
    window_end: str
    severity: str

class SignalResponse(BaseModel):
    window_end: str
    generated_at: str
    top_hub_signals: List[TopHubSignal]

def severity(score: float) -> str:
    if score >= 4.0:
        return "critical"
    if score >= 3.5:
        return "high"
    return "medium"

@app.get("/api/v1/cost-anomaly/signal/top-hub", response_model=SignalResponse)
async def get_top_hub_signals(window_end: Optional[str] = None, limit: int = 5):
    if limit < 1 or limit > 20:
        raise HTTPException(status_code=400, detail="limit must be 1-20")

    # Normalize window_end to last completed hour in UTC
    if window_end:
        try:
            end_dt = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid window_end")
    else:
        now = datetime.now(timezone.utc)
        end_dt = now.replace(minute=0, second=0, microsecond=0) - (now.hour - now.hour)  # last completed hour
        if now.minute < 5:
            end_dt = end_dt.replace(hour=end_dt.hour - 1)

    window_start = end_dt.replace(hour=end_dt.hour - 1)

    try:
        conn = await asyncpg.connect(dsn=DB_URL)
        rows = await conn.fetch(
            """
            SELECT hub_id, cost, expected_cost,
                   (cost - expected_cost) / NULLIF(expected_cost, 0) * 100 AS cost_delta_pct,
                   anomaly_score,
                   $1::text AS window_start, $2::text AS window_end
            FROM hub_cost_metrics
            WHERE window_start = $1 AND window_end = $2
              AND anomaly_score >= 3.5
              AND (cost - expected_cost) / NULLIF(expected_cost, 0) >= 0.20
            ORDER BY anomaly_score * cost DESC
            LIMIT $3
            """,
            window_start.isoformat(), end_dt.isoformat(), limit
        )
        await conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail="data retrieval error") from e

    signals = [
        TopHubSignal(
            hub_id=r["hub_id"],
            anomaly_type="cost_spike",
            cost=float(r["cost"]),
            expected_cost=float(r["expected_cost"]),
            cost_delta_pct=float(r["cost_delta_pct"]),
            anomaly_score=float(r["anomaly_score"]),
            window_start=r["window_start"],
            window_end=r["window_end"],
            severity=severity(float(r["anomaly_score"]))
        )
        for r in rows
    ]

    return SignalResponse(
        window_end=end_dt.isoformat(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        top_hub_signals=signals
    )
```

**Deployment (quick)**  
- Package as Lambda + API Gateway or ECS Fargate.  
- Environment variable `DB_URL` for connection.  
- Add API Gateway usage plan for rate limiting (e.g., 100 req/min).  
- Enable CloudWatch Logs and alarms for 5xx errors.

---

### 6. Testing (fast, high-leverage)
- Unit: test severity mapping, param validation.  
- Integration: spin up local Postgres with sample rows; verify ranking and threshold logic.  
- Smoke: deploy to dev and hit endpoint with known data.

---

### 7. Why this synthesis wins
- Combines Candidate 1’s clarity on read-only design and code with Candidate 2’s emphasis on real data sources and error handling, while respecting Candidate 3’s 1-hour constraint.  
- Resolves contradictions:  
  - No
