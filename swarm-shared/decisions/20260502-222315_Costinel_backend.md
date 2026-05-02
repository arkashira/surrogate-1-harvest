# Costinel / backend

## Final Implementation Plan — Costinel Backend (incremental, <2h)

### Highest-value incremental improvement
Add a **read-only discovery signal pipeline** that:
- Detects idle/underutilized resources (AWS EC2, RDS, EBS) from CUR or mock fixtures.
- Produces **deterministic, audit-ready signals** with embedded, actionable context (top connected insight per resource).
- Exposes via `GET /api/discovery/signals` (paginated, filterable) and a CLI `discovery run --env <env>`.
- Persists signals with deterministic IDs and audit metadata; avoids duplicates within the same day.

This directly supports the **Sense + Signal — No Execute** philosophy and gives the frontend an immediate backend API to consume.

---

### Concrete implementation steps (≤2h)

1. Add models: `DiscoverySignal`, `DiscoveryRun`.
2. Add service: `discovery_service.py` (detectors, signal factory, top-insight embedding).
3. Add CLI command: `discovery run --env <env>`.
4. Add FastAPI route: `GET /api/discovery/signals`.
5. Add minimal fixtures/mocks for non-AWS environments.
6. Add tests for signal creation, deterministic IDs, and duplicate avoidance.

---

### Code snippets

#### 1) Models (`models/discovery.py`)
```python
from datetime import datetime
from uuid import uuid4
from sqlmodel import Field, SQLModel
from typing import Literal, Optional, Dict, Any

class DiscoverySignal(SQLModel, table=True):
    __tablename__ = "discovery_signals"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    deterministic_id: str = Field(index=True, nullable=False)

    env: str = Field(index=True, nullable=False)
    cloud: str = Field(index=True, nullable=False)
    resource_type: str = Field(index=True, nullable=False)
    resource_id: str = Field(index=True, nullable=False)

    rule: str = Field(index=True, nullable=False)
    severity: Literal["low", "medium", "high", "critical"] = Field(index=True)
    title: str
    description: str
    recommendation: str
    top_insight: Optional[str] = Field(
        default=None,
        description="Most-connected insight from knowledge-rag/MOC for this resource"
    )
    metadata_: Dict[str, Any] = Field(default_factory=dict, sa_column_kwargs={"name": "metadata"})

    detected_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="discovery_runs.id")

class DiscoveryRun(SQLModel, table=True):
    __tablename__ = "discovery_runs"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    env: str = Field(index=True, nullable=False)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    status: str = Field(default="running")
    signals_count: int = Field(default=0)
    summary: Dict[str, Any] = Field(default_factory=dict)
```

#### 2) Service (`services/discovery_service.py`)
```python
import hashlib
from datetime import datetime
from typing import List, Dict, Any
from models.discovery import DiscoverySignal, DiscoveryRun
from db import get_session
from sqlmodel import select

def deterministic_id(resource_type: str, resource_id: str, rule: str, date_str: str) -> str:
    payload = f"{resource_type}:{resource_id}:{rule}:{date_str}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]

def top_insight_for(resource_type: str, resource_id: str) -> Optional[str]:
    # Placeholder: query knowledge-rag / MOC for the most-connected insight.
    # Return a short, actionable string or None.
    return None

def detect_idle_ec2(session, env: str, lookback_days: int = 7) -> List[Dict[str, Any]]:
    # Placeholder: integrate with AWS CUR or CloudWatch metrics in real usage.
    mock_resources = [
        {"resource_id": "i-0abc123", "avg_cpu": 2.1, "days": 7},
        {"resource_id": "i-0def456", "avg_cpu": 0.8, "days": 7},
    ]
    signals = []
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    for r in mock_resources:
        if r["avg_cpu"] < 5.0:
            signals.append({
                "env": env,
                "cloud": "aws",
                "resource_type": "ec2",
                "resource_id": r["resource_id"],
                "rule": "idle_cpu",
                "severity": "high" if r["avg_cpu"] < 1.0 else "medium",
                "title": f"Idle EC2 instance: {r['resource_id']}",
                "description": f"Average CPU {r['avg_cpu']}% over {r['days']} days.",
                "recommendation": "Consider stopping or downsizing instance after validation.",
                "metadata_": {"avg_cpu": r["avg_cpu"], "lookback_days": lookback_days},
                "deterministic_id": deterministic_id("ec2", r["resource_id"], "idle_cpu", date_str),
                "top_insight": top_insight_for("ec2", r["resource_id"])
            })
    return signals

def detect_orphan_ebs(session, env: str) -> List[Dict[str, Any]]:
    # Placeholder: detect unattached volumes
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    mock = [
        {"resource_id": "vol-0aaa111", "state": "available", "days_unattached": 14}
    ]
    signals = []
    for v in mock:
        signals.append({
            "env": env,
            "cloud": "aws",
            "resource_type": "ebs",
            "resource_id": v["resource_id"],
            "rule": "orphan_ebs",
            "severity": "medium",
            "title": f"Orphan EBS volume: {v['resource_id']}",
            "description": f"Volume unattached for {v['days_unattached']} days.",
            "recommendation": "Snapshot if needed and delete to reduce cost.",
            "metadata_": {"state": v["state"], "days_unattached": v["days_unattached"]},
            "deterministic_id": deterministic_id("ebs", v["resource_id"], "orphan_ebs", date_str),
            "top_insight": top_insight_for("ebs", v["resource_id"])
        })
    return signals

def create_signals(env: str, detectors=None) -> DiscoveryRun:
    if detectors is None:
        detectors = [detect_idle_ec2, detect_orphan_ebs]

    session = next(get_session())
    run = DiscoveryRun(env=env, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    all_signals = []
    for detector in detectors:
        try:
            found = detector(session, env)
            for s in found:
                signal = DiscoverySignal(**s, run_id=run.id)
                exists = session.exec(
                    select(DiscoverySignal).where(DiscoverySignal.deterministic_id == signal.deterministic_id)
                ).first()
                if not exists:
                    session.add(signal)
                    all_signals.append(signal)
        except Exception as exc:
            # Keep pipeline resilient per-detector
            print(f"Detector {detector.__name__} failed: {exc}")

    run.status = "completed"
    run.finished_at = datetime.utcnow()
    run.signals_count = len(all_signals)
    run.summary = {
        "by_severity": _count_by(all_signals, "severity"),
        "by_resource_type": _count_by(all_signals, "resource_type"),
        "by_rule": _count_by(all_signals, "rule")
    }
    session.commit()
    session.refresh(run)
    return run

def _count_by(signals, key):
    out = {}
    for s in signals:
        val = getattr(s, key, None)
        out[val] = out.get(val, 0) + 1
    return out

def list_signals(env: str = None, rule: str = None, severity: str =
