# Costinel / backend

## Decision Log
- **Highest-value incremental improvement (<2h)**: Add a backend orchestration endpoint that runs `granite-business-research.sh`, enriches output with top-hub (MOC) context + related docs via knowledge-RAG, and returns actionable signals for the Costinel dashboard. No schema changes; pure orchestration + enrichment.
- **Why**: Directly applies past patterns (#business-research #knowledge-rag #graph), surfaces MOC hub insights before planning, and produces dashboard-ready signals without touching frontend or infra.

---

## Implementation Plan (Backend-only, ~90 min)

1. **Add orchestration module** (`services/business_research.py`)
   - Runs `granite-business-research.sh` via `subprocess` with proper `SHELL=/bin/bash`
   - Captures stdout/stderr + exit code
   - On success, parses JSON output for topics/entities

2. **Enrich with top-hub (MOC) context**
   - Calls internal knowledge-RAG helper to fetch top-connected hub (MOC) and related docs
   - Merges MOC summary + related docs into research payload

3. **Expose FastAPI endpoint** (`POST /api/business-research`)
   - Request: optional `focus` (string) and `max_related` (int)
   - Response: `{ research, top_hub, related_docs, signals, ts }`
   - Non-blocking: runs sync (fast) and returns 200 with results; on long runs, return 202 + job id (future)

4. **Add signal generator**
   - Maps research + MOC insights into 3–5 actionable signals (title, description, priority, suggested_action)
   - Stores minimal record in `signals/` (JSONL) for audit trail

5. **Wire into dashboard** (lightweight)
   - Add route to fetch latest signals (`GET /api/signals?limit=10`)
   - No frontend changes required for MVP; signals available for downstream consumers

6. **Tests & guards**
   - Ensure script is executable (`chmod +x`)
   - Validate JSON output; fallback to text parsing
   - Log failures with correlation ID

---

## Code Snippets

### 1) services/business_research.py
```python
# services/business_research.py
import json
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from loguru import logger

# Internal knowledge-RAG helpers (assumed present)
from services.knowledge_rag import get_top_hub, get_related_docs

SCRIPT_PATH = Path("/opt/axentx/Costinel/scripts/granite-business-research.sh")
SIGNALS_DIR = Path("/opt/axentx/Costinel/data/signals")
SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def run_granite_business_research(focus: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes granite-business-research.sh and returns parsed output.
    """
    if not SCRIPT_PATH.is_file():
        raise FileNotFoundError(f"Script not found: {SCRIPT_PATH}")

    if not os.access(SCRIPT_PATH, os.X_OK):
        SCRIPT_PATH.chmod(0o755)

    env = os.environ.copy()
    env["SHELL"] = "/bin/bash"

    cmd = [str(SCRIPT_PATH)]
    if focus:
        cmd.extend(["--focus", focus])

    logger.info("Running business research", cmd=cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=SCRIPT_PATH.parent,
        timeout=120,
    )

    output = {
        "cmd": cmd,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    if result.returncode != 0:
        logger.error("Business research failed", **output)
        # Try best-effort parse
        try:
            output["research"] = json.loads(result.stdout)
        except Exception:
            output["research"] = {"raw_stdout": result.stdout, "raw_stderr": result.stderr}
        return output

    try:
        output["research"] = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Non-JSON stdout, wrapping")
        output["research"] = {"raw_stdout": result.stdout, "raw_stderr": result.stderr}

    return output


def enrich_with_top_hub_and_related(
    research: Dict[str, Any], max_related: int = 5
) -> Dict[str, Any]:
    """
    Enrich research payload with top hub (MOC) and related docs.
    """
    # Determine focus from research if not provided
    focus = research.get("primary_topic") or research.get("focus") or "MOC"
    top_hub = get_top_hub(focus=focus)  # expected: {"hub": "MOC", "score": ..., "summary": ...}

    related = get_related_docs(hub=top_hub.get("hub", "MOC"), limit=max_related)
    return {"top_hub": top_hub, "related_docs": related}


def generate_signals(
    research: Dict[str, Any], top_hub: Dict[str, Any], related_docs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Produce actionable signals for dashboard.
    """
    signals = []

    # Signal 1: Top hub insight
    signals.append(
        {
            "id": f"hub-{top_hub.get('hub', 'unknown')}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "type": "hub_insight",
            "title": f"Top hub: {top_hub.get('hub', 'MOC')}",
            "description": top_hub.get("summary", "No summary available"),
            "priority": "high" if top_hub.get("score", 0) > 0.7 else "medium",
            "suggested_action": "Review related docs and consider governance implications for Costinel",
            "context": {"hub": top_hub, "related_count": len(related_docs)},
        }
    )

    # Signal 2: Costinel-specific recommendation from research
    primary_topic = research.get("primary_topic", "cloud cost governance")
    signals.append(
        {
            "id": f"research-{primary_topic.replace(' ', '-')}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "type": "research_insight",
            "title": f"Research focus: {primary_topic}",
            "description": research.get("summary", research.get("raw_stdout", "")[:200]),
            "priority": "medium",
            "suggested_action": "Evaluate impact on current cost governance policies",
            "context": {"research_focus": primary_topic},
        }
    )

    # Signal 3: Related doc highlights
    for doc in related_docs[:3]:
        signals.append(
            {
                "id": f"doc-{doc.get('slug', 'unknown')}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "type": "related_doc",
                "title": doc.get("title", "Related document"),
                "description": doc.get("summary", "")[:200],
                "priority": "low",
                "suggested_action": "Read for additional context",
                "context": {"doc": doc},
            }
        )

    return signals


def persist_signals(signals: List[Dict[str, Any]]) -> Path:
    """
    Append signals to a JSONL file for audit trail.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_file = SIGNALS_DIR / f"signals-{ts}.jsonl"
    with out_file.open("a", encoding="utf-8") as f:
        for s in signals:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    return out_file


def run_research_pipeline(focus: Optional[str] = None, max_related: int = 5) -> Dict[str, Any]:
    """
    Full pipeline: run research → enrich → generate signals → persist.
    Returns payload suitable for API response.
    """
    research_result = run_granite_business_research(focus=focus)
    research = research_result.get("research
