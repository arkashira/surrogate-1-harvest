# airship / discovery

## Final synthesized solution (correct + actionable)

**Core problem**: research runs (granite-business-research) and knowledge-graph queries (knowledge-rag) are not composed into an automated discovery flow, so teams cannot reliably surface the highest-centrality hub and related docs at runtime.

**Goal**: deliver a single, reliable `GET /api/v1/discovery/top-hub` and matching CLI that:
- runs research once per day (if needed),
- queries the graph for the top hub + related docs,
- returns a compact, contract-first JSON payload,
- degrades gracefully if scripts change,
- is verifiable in CI/local runs.

---

### 1. Contract (what the system returns)

```json
{
  "ts": "2025-01-01T12:00:00Z",
  "hub": {
    "id": "MOC",
    "label": "Market Operating Context",
    "type": "hub",
    "centrality": { "degree": 42, "normalized": 0.93 }
  },
  "related_docs": [
    {
      "id": "doc-123",
      "title": "Q4 Market Signals",
      "summary": "Top drivers for Q4...",
      "score": 0.87
    }
  ],
  "meta": {
    "research_run": true,
    "research_ts": "2025-01-01T06:00:00Z",
    "cached": false
  }
}
```

Rules:
- `hub` must be non-null when data exists; if unavailable, return clear error (503 with diagnostic).
- `related_docs` is always an array (empty if none).
- `centrality` is normalized 0–1 when possible; raw value included.

---

### 2. Discovery service (single source of truth)

`/opt/axentx/airship/arkship/services/discovery.py`

```python
import subprocess
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

RESEARCH_SCRIPT = Path("/opt/axentx/airship/scripts/granite-business-research.sh")
KNOWLEDGE_RAG = Path("/opt/axentx/airship/scripts/knowledge-rag")
CACHE_DIR = Path("/opt/axentx/airship/tmp/discovery")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{_today_key()}-{name}.json"

def _run(cmd: List[str], *, cwd: Path, desc: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={**os.environ, "SHELL": "/bin/bash"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"{desc} failed: {result.stderr.strip()}")
    return result

def run_granite_research(*, force: bool = False) -> Dict[str, Any]:
    """Run research once per day unless already run (unless force)."""
    meta_path = _cache_path("research-meta")
    if not force and meta_path.exists():
        return json.loads(meta_path.read_text())

    if not RESEARCH_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing research script: {RESEARCH_SCRIPT}")

    _run(
        ["bash", str(RESEARCH_SCRIPT)],
        cwd=RESEARCH_SCRIPT.parent.parent,
        desc="granite-business-research",
    )

    meta = {
        "ts": _utc_now_iso(),
        "script": str(RESEARCH_SCRIPT),
        "exit_code": 0,
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta

def query_top_hub(*, limit: int = 5) -> Dict[str, Any]:
    """
    Query knowledge-rag for top hub + related docs.
    Supports two known interfaces:
      1) knowledge-rag top-hub --limit N --json
      2) knowledge-rag query "top hub" --json
    """
    project_root = KNOWLEDGE_RAG.parent.parent

    # Preferred interface
    cmd = ["bash", str(KNOWLEDGE_RAG), "top-hub", "--limit", str(limit), "--json"]
    try:
        result = _run(cmd, cwd=project_root, desc="knowledge-rag top-hub")
    except RuntimeError:
        # Fallback interface
        cmd = ["bash", str(KNOWLEDGE_RAG), "query", "top hub", "--json"]
        result = _run(cmd, cwd=project_root, desc="knowledge-rag query top hub")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"knowledge-rag returned non-JSON: {exc}") from exc

    # Normalize payload
    hub_raw = payload.get("hub") or payload.get("top_hub") or payload.get("node")
    centrality_raw = payload.get("centrality") or payload.get("score") or payload.get("degree") or 0
    related_raw = payload.get("related_docs") or payload.get("docs") or payload.get("related") or []

    if isinstance(centrality_raw, dict):
        degree = centrality_raw.get("degree") or centrality_raw.get("value") or 0
        normalized = centrality_raw.get("normalized") or (float(degree) / 100.0 if degree else 0.0)
    else:
        degree = float(centrality_raw) if centrality_raw else 0.0
        normalized = min(max(degree / 100.0, 0.0), 1.0)

    hub_label = "unknown"
    hub_id = "unknown"
    if isinstance(hub_raw, dict):
        hub_id = str(hub_raw.get("id") or hub_raw.get("name") or hub_raw.get("label") or "unknown")
        hub_label = str(hub_raw.get("label") or hub_raw.get("title") or hub_id)
    elif isinstance(hub_raw, str):
        hub_id = hub_raw
        hub_label = hub_raw

    related_docs: List[Dict[str, Any]] = []
    for item in related_raw[:limit]:
        if isinstance(item, dict):
            related_docs.append(
                {
                    "id": str(item.get("id") or item.get("doc_id") or ""),
                    "title": str(item.get("title") or item.get("label") or "Untitled"),
                    "summary": str(item.get("summary") or item.get("snippet") or ""),
                    "score": float(item.get("score") or item.get("relevance") or 0.0),
                }
            )
        else:
            related_docs.append({"id": "", "title": str(item), "summary": "", "score": 0.0})

    out = {
        "ts": _utc_now_iso(),
        "hub": {
            "id": hub_id,
            "label": hub_label,
            "type": "hub",
            "centrality": {"degree": degree, "normalized": normalized},
        },
        "related_docs": related_docs,
        "meta": {
            "research_run": False,
            "research_ts": None,
            "cached": False,
        },
    }

    cache_path = _cache_path("top-hub")
    cache_path.write_text(json.dumps(out, indent=2))
    return out

def get_top_hub(*, limit: int = 5, refresh: bool = False) -> Dict[str, Any]:
    """End-to-end discovery: research (if needed) -> top hub -> payload."""
    if refresh or not _cache_path("top-hub").exists():
        run_granite_research(force=refresh)
        result = query_top_hub(limit=limit)
        result["meta"]["research_run"] = True
        result["meta"]["research_ts"] = _utc_now_iso()
        result["meta"]["cached"] = False
        _cache_path("top-hub").write_text(json.dumps(result, indent=2))
        return result

    # Return cached, but ensure research meta exists for
