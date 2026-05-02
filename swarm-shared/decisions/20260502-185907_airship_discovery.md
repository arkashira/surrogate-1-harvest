# airship / discovery

## Incremental Improvement: Zero-config discovery CLI (`airship discover`)

**Value**: <2h implementation that operationalizes past patterns (business research + knowledge-rag + top-hub insight) into a single command that surfaces strategic context for the Arkship platform.

---

### Implementation Plan (90 minutes)

1. **Create CLI entrypoint** (`/opt/axentx/airship/airship`) — Python click-based dispatcher (5 min)
2. **Implement `discover` command** — orchestrates research → RAG → hub insight (45 min)
3. **Add knowledge-rag integration** — reuses existing Neo4j/Qdrant setup to query top hub + docs (20 min)
4. **Handle script execution safely** — proper shebang, executable checks, SHELL env (10 min)
5. **Polish output** — formatted markdown report with actionable insights (10 min)

---

### Code Implementation

#### 1. Main CLI Entrypoint (`/opt/axentx/airship/airship`)

```python
#!/usr/bin/env python3
"""
Arkship Discovery CLI
Zero-config context surfacing for platform insights.
"""
import os
import sys
import subprocess
import json
from datetime import datetime
from pathlib import Path

try:
    import click
except ImportError:
    print("Installing click dependency...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "click", "-q"])
    import click

PROJECT_ROOT = Path(__file__).parent

@click.group()
def cli():
    """Arkship Platform CLI - DevOps automation with AI assistance."""
    pass

@cli.command()
@click.option("--output", "-o", type=click.Path(), help="Output file (markdown)")
@click.option("--no-research", is_flag=True, help="Skip granite-business-research")
@click.option("--no-graph", is_flag=True, help="Skip knowledge graph query")
def discover(output, no_research, no_graph):
    """
    Run zero-config discovery:
    1. Execute granite-business-research.sh (if present)
    2. Query knowledge graph for top hub + related docs
    3. Generate actionable insights report
    """
    report = []
    report.append(f"# Arkship Discovery Report")
    report.append(f"**Generated:** {datetime.utcnow().isoformat()} UTC\n")
    
    # Step 1: Business Research
    if not no_research:
        research_result = run_granite_research()
        report.append("## 📊 Market Intelligence")
        report.append(research_result)
        report.append("")
    
    # Step 2: Knowledge Graph Query
    if not no_graph:
        graph_result = query_knowledge_graph()
        report.append("## 🕸️ Knowledge Graph Insights")
        report.append(graph_result)
        report.append("")
    
    # Step 3: Top Hub Focus
    hub_result = get_top_hub_insight()
    report.append("## 🎯 Top Hub Focus")
    report.append(hub_result)
    report.append("")
    
    # Step 4: Action Items
    report.append("## 🚀 Recommended Actions")
    report.append(generate_actions())
    
    full_report = "\n".join(report)
    
    if output:
        with open(output, "w") as f:
            f.write(full_report)
        click.echo(f"✓ Report written to {output}")
    else:
        click.echo(full_report)

def run_granite_research():
    """Execute granite-business-research.sh with proper error handling."""
    script_path = PROJECT_ROOT / "granite-business-research.sh"
    
    if not script_path.exists():
        return "_No granite-business-research.sh found — skipping market analysis._"
    
    # Ensure executable
    if not os.access(script_path, os.X_OK):
        script_path.chmod(0o755)
    
    # Set proper shell environment per pattern
    env = os.environ.copy()
    env["SHELL"] = "/bin/bash"
    
    try:
        result = subprocess.run(
            ["/bin/bash", str(script_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
            timeout=120
        )
        
        if result.returncode == 0:
            return result.stdout.strip() or "_Research completed (no output)._"
        else:
            return f"_⚠️ Research script exited {result.returncode}: {result.stderr[:200]}_"
    
    except subprocess.TimeoutExpired:
        return "_⚠️ Research script timed out after 120s._"
    except Exception as e:
        return f"_⚠️ Research execution error: {e}_"

def query_knowledge_graph():
    """Query Neo4j for top-connected hub and related documents."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return "_Neo4j driver not available — install with `pip install neo4j`_"
    
    # Neo4j connection (defaults from docker-compose)
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        
        with driver.session() as session:
            # Find most-connected hub (highest degree)
            result = session.run("""
                MATCH (h:Hub)
                OPTIONAL MATCH (h)-[r]-(related)
                WITH h, count(r) as connections, collect(DISTINCT related.name)[..5] as top_related
                ORDER BY connections DESC
                LIMIT 1
                RETURN h.name as hub, connections, top_related
            """)
            
            record = result.single()
            if not record:
                return "_No hubs found in knowledge graph._"
            
            hub = record["hub"]
            connections = record["connections"]
            related = record["top_related"] or []
            
            # Get top 5 related docs
            docs_result = session.run("""
                MATCH (h:Hub {name: $hub})-[:REFERENCES|USES|MENTIONS]-(doc:Document)
                RETURN doc.title as title, doc.type as type, doc.relevance as score
                ORDER BY score DESC
                LIMIT 5
            """, hub=hub)
            
            docs = [dict(d) for d in docs_result]
            
            lines = [
                f"**Top Hub:** `{hub}` ({connections} connections)",
                "",
                "**Most Related Entities:**",
            ]
            for i, entity in enumerate(related, 1):
                lines.append(f"  {i}. `{entity}`")
            
            if docs:
                lines.extend(["", "**Key Documents:**"])
                for doc in docs:
                    lines.append(f"  - **{doc['title']}** ({doc.get('type', 'doc')})")
            
            return "\n".join(lines)
    
    except Exception as e:
        return f"_⚠️ Graph query error: {e}_"
    finally:
        if 'driver' in locals():
            driver.close()

def get_top_hub_insight():
    """Pattern: Review most-connected hub before planning (MOC pattern)."""
    try:
        from neo4j import GraphDatabase
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        driver = GraphDatabase.driver(uri, auth=(
            os.getenv("NEO4J_USER", "neo4j"),
            os.getenv("NEO4J_PASSWORD", "password")
        ))
        
        with driver.session() as session:
            result = session.run("""
                MATCH (h:Hub)-[r]-(related)
                WITH h, count(r) as degree
                ORDER BY degree DESC
                LIMIT 1
                RETURN h.name as name, h.description as desc, degree
            """)
            
            record = result.single()
            if record:
                return (
                    f"Focus on **{record['name']}** hub "
                    f"({record['degree']} connections) — "
                    f"{record.get('desc', 'central coordination point')}. "
                    f"Review related workflows and blueprint dependencies before planning."
                )
    except:
        pass
    
    return "Run `knowledge-rag` to populate graph with
