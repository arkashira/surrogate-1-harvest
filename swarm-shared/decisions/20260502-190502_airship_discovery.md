# airship / discovery

## Incremental Improvement: Zero-config discovery CLI (`airship discover`)

**Value**: <2h implementation that operationalizes past patterns (business research + knowledge-rag + top-hub insight) into a single command that surfaces strategic context for the Arkship/Surrogate platform.

---

## Implementation Plan (≤2h)

### 1. CLI Entrypoint (15 min)
- Create `/opt/axentx/airship/airship` (executable Python CLI)
- Add `discover` subcommand with zero-config defaults

### 2. Business Research Pipeline (30 min)
- Wrap `granite-business-research.sh` execution with proper shebang/exec handling
- Capture output → structured JSON for downstream consumption

### 3. Knowledge-RAG Query (30 min)
- Execute knowledge-rag against top hub (MOC) and related docs
- Use Neo4j connection from Surrogate service (port 8001) or local graph
- Extract top 5 insights with confidence scores

### 4. Top-Hub Insight Synthesis (20 min)
- Review most-connected hub (MOC) before generating report
- Cross-reference with Arkship/Surrogate architecture patterns
- Generate actionable recommendations

### 5. Output Formatting (15 min)
- Markdown report with sections: Market Context, Knowledge Graph Insights, Top Hub Analysis, Recommended Actions
- Color-coded terminal output with optional `--json` flag

### 6. Integration & Testing (20 min)
- Ensure proper error handling for missing scripts/services
- Add to `PATH` via symlink or shell completion
- Test full flow end-to-end

---

## Code Implementation

### `/opt/axentx/airship/airship`
```python
#!/usr/bin/env python3
"""
Arkship Discovery CLI - Zero-config strategic context surfacing
Operationalizes: business research + knowledge-rag + top-hub insight
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

def run_granite_research() -> dict:
    """Execute granite-business-research.sh with proper Bash handling."""
    script = PROJECT_ROOT / "scripts" / "granite-business-research.sh"
    if not script.exists():
        return {"error": "granite-business-research.sh not found", "data": None}
    
    try:
        result = subprocess.run(
            ["/bin/bash", str(script)],
            capture_output=True,
            text=True,
            timeout=300,
            env={**subprocess.os.environ, "SHELL": "/bin/bash"}
        )
        if result.returncode != 0:
            return {"error": result.stderr, "data": None}
        return {"error": None, "data": result.stdout}
    except subprocess.TimeoutExpired:
        return {"error": "Research script timed out", "data": None}

def query_knowledge_rag(top_hub: str = "MOC") -> list:
    """Query knowledge-rag for top hub and related docs."""
    # Try to connect to Surrogate service (port 8001) or use local fallback
    try:
        import requests
        resp = requests.post(
            "http://localhost:8001/api/knowledge/query",
            json={"hub": top_hub, "limit": 5, "include_related": True},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("insights", [])
    except Exception:
        pass
    
    # Fallback: read from pre-generated insights file
    insights_file = PROJECT_ROOT / "data" / "knowledge_rag_insights.json"
    if insights_file.exists():
        with open(insights_file) as f:
            data = json.load(f)
            return data.get(top_hub, [])
    
    return [{"insight": "Knowledge graph unavailable - run surrogate service", "confidence": 0.0}]

def get_top_hub_analysis() -> dict:
    """Review most-connected hub (MOC) for strategic insights."""
    # Pattern: top-hub doc insight (2026-04-27)
    moc_file = PROJECT_ROOT / "docs" / "knowledge" / "MOC.md"
    if moc_file.exists():
        with open(moc_file) as f:
            content = f.read()
        return {
            "hub": "MOC",
            "connections": content.count("\n## ") or 10,  # estimate
            "key_themes": extract_themes(content),
            "strategic_value": "high"
        }
    
    return {
        "hub": "MOC",
        "connections": 0,
        "key_themes": ["DevOps", "AI", "Infrastructure"],
        "strategive_value": "medium"
    }

def extract_themes(content: str) -> list:
    """Extract key themes from hub content."""
    themes = []
    if "infrastructure" in content.lower():
        themes.append("Infrastructure Automation")
    if "ai" in content.lower() or "surrogate" in content.lower():
        themes.append("AI-Driven Operations")
    if "workflow" in content.lower() or "temporal" in content.lower():
        themes.append("Workflow Orchestration")
    if "incident" in content.lower():
        themes.append("Incident Management")
    return themes or ["General DevOps"]

def generate_report(research: dict, insights: list, hub_analysis: dict, json_output: bool = False):
    """Generate discovery report."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if json_output:
        report = {
            "timestamp": timestamp,
            "research": research,
            "knowledge_insights": insights,
            "top_hub_analysis": hub_analysis,
            "recommendations": generate_recommendations(insights, hub_analysis)
        }
        print(json.dumps(report, indent=2))
        return
    
    # Markdown terminal output
    print(f"\n{'='*60}")
    print(f"  ARKSHIP DISCOVERY REPORT - {timestamp}")
    print(f"{'='*60}\n")
    
    print("📊 MARKET CONTEXT")
    print("-" * 40)
    if research.get("data"):
        print(research["data"][:500] + "..." if len(research["data"]) > 500 else research["data"])
    else:
        print("⚠️  Research unavailable:", research.get("error", "Unknown error"))
    
    print(f"\n🧠 KNOWLEDGE GRAPH INSIGHTS (Top Hub: {hub_analysis['hub']})")
    print("-" * 40)
    for i, insight in enumerate(insights[:5], 1):
        conf = insight.get("confidence", 0)
        bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
        print(f"{i}. {insight.get('insight', 'N/A')}")
        print(f"   Confidence: {bar} {conf:.0%}")
    
    print(f"\n🎯 TOP HUB ANALYSIS: {hub_analysis['hub']}")
    print("-" * 40)
    print(f"Connections: {hub_analysis['connections']}")
    print(f"Strategic Value: {hub_analysis['strategic_value'].upper()}")
    print(f"Key Themes: {', '.join(hub_analysis['key_themes'])}")
    
    print(f"\n✅ RECOMMENDED ACTIONS")
    print("-" * 40)
    for rec in generate_recommendations(insights, hub_analysis):
        print(f"• {rec}")
    
    print(f"\n{'='*60}\n")

def generate_recommendations(insights: list, hub_analysis: dict) -> list:
    """Generate actionable recommendations."""
    recs = []
    
    if "AI-Driven Operations" in hub_analysis["key_themes"]:
        recs.append("Deploy Surrogate AI service for DevOps assistance (port 8001)")
    
    if "Infrastructure Automation" in hub_analysis["key_themes"]:
        recs.append("Review Arkship blueprints for IaC automation opportunities")
    
    if any("temporal" in str(i).lower() for i in insights):
        recs.append("Enable Temporal workflow orchestration for complex deployments")
    
    if not recs:
        recs.append("Run full Arkship + Surrogate stack
