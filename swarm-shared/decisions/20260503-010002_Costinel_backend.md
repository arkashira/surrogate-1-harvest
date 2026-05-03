# Costinel / backend

## Implementation Plan: Knowledge-RAG Pipeline for Business Research

**Scope**: Backend service that runs `granite-business-research.sh` → extracts top hub → queries related docs → stores signal for Costinel dashboard.  
**Time**: ≤2h | **Risk**: Low (read-only, no infra changes) | **Value**: Enables contextual cost-governance signals from business research.

---

### 1. Architecture (Backend-only)

```
/opt/axentx/Costinel/
├── services/
│   └── knowledge_rag/
│       ├── __init__.py
│       ├── pipeline.py          # orchestrator
│       ├── research_runner.py   # granite-business-research.sh wrapper
│       ├── graph_query.py       # top-hub + related docs
│       └── signal_store.py      # writes to signals/
├── signals/                     # committed outputs (dashboard reads)
│   └── top_hub_signal.json
└── scripts/
    └── granite-business-research.sh
```

---

### 2. Concrete Implementation

#### 2.1 Research Runner (Bash wrapper with correct shebang + executable)

```bash
#!/usr/bin/env bash
# scripts/granite-business-research.sh
set -euo pipefail
SHELL=/bin/bash

# Placeholder: real research logic goes here
# For now, emit structured JSON that pipeline can consume
cat <<'EOF'
{
  "timestamp": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
  "topics": ["cloud cost governance", "reserved instance optimization", "multi-cloud tagging"],
  "entities": ["AWS", "GCP", "FinOps"],
  "raw_text": "Granite business research output..."
}
EOF
```

```bash
chmod +x scripts/granite-business-research.sh
```

---

#### 2.2 Graph Query Module (top-hub + related docs)

```python
# services/knowledge_rag/graph_query.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Any

GRAPH_DIR = Path(__file__).parent.parent.parent / "knowledge_graph"

def load_graph() -> Dict[str, Any]:
    graph_file = GRAPH_DIR / "graph.json"
    if not graph_file.exists():
        return {"nodes": [], "edges": []}
    return json.loads(graph_file.read_text())

def top_hub() -> Dict[str, Any]:
    """
    Returns the most-connected hub node.
    Pattern: #knowledge-rag #graph #hub
    """
    graph = load_graph()
    degree = {}
    for edge in graph.get("edges", []):
        a, b = edge.get("source"), edge.get("target")
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    if not degree:
        return {"id": "MOC", "label": "MOC", "degree": 0}

    hub_id = max(degree, key=degree.get)
    hub_node = next((n for n in graph.get("nodes", []) if n.get("id") == hub_id), {"id": hub_id})
    return {**hub_node, "degree": degree[hub_id]}

def related_docs(hub_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Returns documents connected to hub.
    """
    graph = load_graph()
    related = []
    seen = set()
    for edge in graph.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        if src == hub_id and tgt not in seen:
            node = next((n for n in graph.get("nodes", []) if n.get("id") == tgt), None)
            if node and node.get("type") == "doc":
                related.append(node)
                seen.add(tgt)
        elif tgt == hub_id and src not in seen:
            node = next((n for n in graph.get("nodes", []) if n.get("id") == src), None)
            if node and node.get("type") == "doc":
                related.append(node)
                seen.add(src)
        if len(related) >= limit:
            break
    return related
```

---

#### 2.3 Signal Store (dashboard-readable)

```python
# services/knowledge_rag/signal_store.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SIGNALS_DIR = Path(__file__).parent.parent.parent / "signals"
SIGNALS_DIR.mkdir(exist_ok=True)

def write_top_hub_signal(hub: dict, related: list, research_meta: dict) -> Path:
    payload = {
        "signal_type": "top_hub",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research": research_meta,
        "hub": hub,
        "related_docs": related,
        "tags": ["knowledge-rag", "graph", "hub", "business-research"],
        "dashboard_card": {
            "title": f"Top Hub: {hub.get('label', hub.get('id', 'N/A'))}",
            "summary": f"Degree {hub.get('degree', 0)} — {len(related)} related docs",
            "cta": "Review context before cost governance decisions"
        }
    }
    out = SIGNALS_DIR / "top_hub_signal.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out
```

---

#### 2.4 Orchestrator Pipeline

```python
# services/knowledge_rag/pipeline.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .research_runner import run_research
from .graph_query import top_hub, related_docs
from .signal_store import write_top_hub_signal

def run_knowledge_rag_pipeline() -> dict:
    """
    Executes:
    1) granite-business-research.sh
    2) extract top hub
    3) query related docs
    4) store signal for dashboard
    """
    # 1) Run research
    research_out = run_research()

    # 2) Top hub
    hub = top_hub()

    # 3) Related docs
    related = related_docs(hub_id=hub["id"])

    # 4) Store signal
    signal_path = write_top_hub_signal(
        hub=hub,
        related=related,
        research_meta=research_out
    )

    return {
        "status": "ok",
        "hub": hub,
        "related_docs_count": len(related),
        "signal_path": str(signal_path)
    }

# research_runner.py helper
def run_research() -> dict:
    script = Path(__file__).parent.parent.parent / "scripts" / "granite-business-research.sh"
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        check=True
    )
    return json.loads(result.stdout.strip())
```

---

#### 2.5 CLI Entrypoint (for cron / manual runs)

```python
# services/knowledge_rag/__main__.py
from __future__ import annotations

from .pipeline import run_knowledge_rag_pipeline

if __name__ == "__main__":
    result = run_knowledge_rag_pipeline()
    print(json.dumps(result, indent=2))
```

```bash
# Make it runnable
chmod +x services/knowledge_rag/__main__.py 2>/dev/null || true
```

---

#### 2.6 Cron Setup (with SHELL=/bin/bash)

```bash
# crontab -e
SHELL=/bin/bash
# Run business research + knowledge-rag every 6 hours
0 */6 * * * /usr/bin/env bash /opt/axentx/Costinel/services/knowledge_rag/__main__.py >> /var/log/costinel_knowledge_rag.log 2>&1
```

---

### 3. Dashboard Consumption (Frontend — read-only)

The dashboard can read `/signals/top_hub_signal.json` (static file served by backend or nginx) and render a card:

```
Top Hub: MOC
Degree 12 — 5 related docs
[Review Context]  → opens modal with related docs list
```

No API/auth changes required.


