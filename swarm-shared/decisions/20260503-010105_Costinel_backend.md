# Costinel / backend

## Final Implementation Plan: Knowledge-RAG Pipeline for Business Research

**Scope**: Backend-only orchestration service that executes `granite-business-research.sh`, enriches results with top-hub (MOC) context and related documents, and returns actionable insights. No schema changes, no UI, pure backend.

**Why this**: Combines the strongest elements from both candidates: Candidate 1’s concrete code structure and Candidate 2’s emphasis on RAG integration. Delivers immediate value by connecting market analysis to the most-connected hub (MOC) for contextual decision support. Time: ~90–120 minutes.

---

### 1. Architecture (backend-only)

```
Costinel/
└── services/
    └── knowledge_rag/
        ├── __init__.py
        ├── orchestrator.py      # main pipeline
        ├── market_analyzer.py   # runs granite-business-research.sh
        ├── top_hub_client.py    # queries top-hub (MOC) + related docs
        └── models.py            # Pydantic models
```

---

### 2. Code Implementation

#### `services/knowledge_rag/models.py`

```python
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional

class RelatedDoc(BaseModel):
    doc_id: str
    title: str
    hub: str
    score: float
    snippet: str
    url: Optional[str] = None

class TopHubInsight(BaseModel):
    hub_id: str
    hub_name: str
    centrality_score: float
    description: str
    related_docs: List[RelatedDoc]

class MarketAnalysisResult(BaseModel):
    report_id: str
    generated_at: datetime
    summary: str
    key_findings: List[str]
    top_hub_insight: Optional[TopHubInsight] = None
```

---

#### `services/knowledge_rag/market_analyzer.py`

```python
import subprocess
import json
import logging
from pathlib import Path
from datetime import datetime
from .models import MarketAnalysisResult

logger = logging.getLogger(__name__)

REPORT_DIR = Path("/opt/axentx/Costinel/reports/market")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

def run_granite_business_research(topic: str) -> MarketAnalysisResult:
    """
    Executes granite-business-research.sh and returns structured result.
    Ensures proper executable permissions and Bash invocation.
    """
    script_path = "/opt/axentx/Costinel/scripts/granite-business-research.sh"
    
    # Ensure executable
    Path(script_path).chmod(0o755)
    
    cmd = ["bash", script_path, topic]
    logger.info(f"Running market analysis: {' '.join(cmd)}")
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env={**subprocess.os.environ, "SHELL": "/bin/bash"}
    )
    
    if result.returncode != 0:
        logger.error(f"Script failed: {result.stderr}")
        raise RuntimeError(f"Market analysis failed: {result.stderr}")
    
    # Parse JSON output from script (assumes script prints JSON to stdout)
    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        # Fallback: wrap raw output
        data = {
            "report_id": f"granite-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "summary": result.stdout.strip()[:500],
            "key_findings": [result.stdout.strip()[:200]],
            "top_hub": "MOC",
            "raw_output": result.stdout
        }
    
    # Build minimal MarketAnalysisResult (top_hub_insight populated by caller)
    return MarketAnalysisResult(
        report_id=data.get("report_id", f"granite-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"),
        generated_at=datetime.utcnow(),
        summary=data.get("summary", "No summary available"),
        key_findings=data.get("key_findings", []),
        top_hub_insight=None  # To be filled by top_hub_client
    )
```

---

#### `services/knowledge_rag/top_hub_client.py`

```python
import httpx
import logging
from .models import TopHubInsight, RelatedDoc

logger = logging.getLogger(__name__)

# Static top-hub knowledge (MOC) - aligns with pattern #knowledge-rag #graph #hub
# In production, this would query a graph DB or RAG service.
STATIC_TOP_HUB = TopHubInsight(
    hub_id="MOC",
    hub_name="Market Opportunity Canvas",
    centrality_score=0.94,
    description=(
        "The most-connected hub in Costinel's knowledge graph. "
        "Used to contextualize cloud cost governance decisions with market signals."
    ),
    related_docs=[
        RelatedDoc(
            doc_id="costinel-ri-2026",
            title="Reserved Instance Strategy 2026",
            hub="MOC",
            score=0.91,
            snippet="Align RI purchases with forecasted demand spikes identified in market analysis."
        ),
        RelatedDoc(
            doc_id="cloud-governance-framework",
            title="Cloud Governance Framework v3",
            hub="MOC",
            score=0.87,
            snippet="Governance gates triggered by market volatility signals."
        ),
        RelatedDoc(
            doc_id="aws-cost-anomaly-detection",
            title="AWS Cost Anomaly Detection Playbook",
            hub="MOC",
            score=0.83,
            snippet="Use MOC insights to prioritize anomaly investigations by market impact."
        )
    ]
)

async def get_top_hub_insight(hub_id: str = "MOC") -> TopHubInsight:
    """
    Returns top-hub insight. Currently static (MOC) per pattern.
    Future: query RAG service or graph DB.
    """
    logger.info(f"Querying top-hub: {hub_id}")
    if hub_id != "MOC":
        logger.warning(f"Requested hub {hub_id} not in static registry, returning MOC")
    return STATIC_TOP_HUB

async def enrich_with_related_docs(analysis: MarketAnalysisResult) -> MarketAnalysisResult:
    """Enriches market analysis with top-hub and related docs."""
    top_hub = await get_top_hub_insight("MOC")
    analysis.top_hub_insight = top_hub
    return analysis
```

---

#### `services/knowledge_rag/orchestrator.py`

```python
import logging
from datetime import datetime
from .market_analyzer import run_granite_business_research
from .top_hub_client import enrich_with_related_docs
from .models import MarketAnalysisResult

logger = logging.getLogger(__name__)

async def run_knowledge_rag_pipeline(topic: str = "cloud cost governance market trends") -> MarketAnalysisResult:
    """
    Orchestrator for Knowledge-RAG pipeline:
    1. Run granite-business-research.sh
    2. Query top-hub (MOC) + related docs
    3. Return enriched insight
    """
    logger.info(f"Starting Knowledge-RAG pipeline for topic: {topic}")
    
    # Step 1: Market analysis
    analysis = run_granite_business_research(topic)
    
    # Step 2: Enrich with top-hub insight
    enriched = await enrich_with_related_docs(analysis)
    
    logger.info(
        f"Pipeline complete. Report: {enriched.report_id}, "
        f"Hub: {enriched.top_hub_insight.hub_name if enriched.top_hub_insight else 'N/A'}"
    )
    
    return enriched

# CLI entrypoint for testing
if __name__ == "__main__":
    import asyncio
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    topic = sys.argv[1] if len(sys.argv) > 1 else "cloud cost optimization"
    result = asyncio.run(run_knowledge_rag_pipeline(topic))
    
    print("\n=== Knowledge-RAG Result ===")
    print(f"Report ID: {result.report_id}")
    print(f"Summary: {result.summary}")
    if result.top_hub_insight:
        print(f"\nTop Hub: {result.top_hub_insight.hub_name} (score: {result.top_hub_insight.centrality
