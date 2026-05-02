# airship / discovery

## Highest-value incremental improvement
Add a zero-config discovery CLI (`airship discover`) that:
1. Runs `granite-business-research.sh` (if present) to refresh market insights
2. Queries the knowledge graph via `knowledge-rag` to surface the most-connected hub (e.g., MOC) and top related docs
3. Emits a concise, actionable summary (<30s runtime) to stdout and optionally saves to `discovery/YYYYMMDD_HHMMSS.md`

This directly addresses the past pattern: review the most-connected hub before planning tasks and composes research + RAG into an automated discovery flow.

---

## Implementation plan (<2h)

1. Create `bin/airship-discover` (Bash, executable) — orchestrates the flow with safe fallbacks
2. Add lightweight Python helper `tools/rag_top_hub.py` to query Neo4j (or fallback to local search) and return top hub + related docs
3. Wire into repo: ensure `knowledge-rag` and `granite-business-research.sh` are callable or provide no-op stubs if absent
4. Add `discovery/` to `.gitignore` (artifacts)
5. Smoke test end-to-end

---

## Code snippets

### bin/airship-discover
```bash
#!/usr/bin/env bash
# airship discover — zero-config discovery CLI
# Usage: ./bin/airship-discover [--save]
#   --save   write discovery/YYYYMMDD_HHMMSS.md (default: stdout only)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SAVE=false
if [[ "${1:-}" == "--save" ]]; then
  SAVE=true
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR="discovery"
mkdir -p "$OUT_DIR"

log() { echo "[airship/discover] $*"; }

# 1) Run market research if available
RESEARCH_SCRIPT="scripts/granite-business-research.sh"
if [[ -x "$RESEARCH_SCRIPT" ]]; then
  log "Running market analysis ($RESEARCH_SCRIPT)..."
  bash "$RESEARCH_SCRIPT" || log "WARN: research script exited non-zero (continuing)"
else
  log "No executable $RESEARCH_SCRIPT — skipping market analysis"
fi

# 2) Query top hub + related docs via RAG helper
RAG_HELPER="tools/rag_top_hub.py"
if [[ -x "$RAG_HELPER" || -f "$RAG_HELPER" ]]; then
  log "Querying knowledge graph for top hub and related docs..."
  RAG_OUTPUT=$(python "$RAG_HELPER" 2>/dev/null || echo "ERROR: RAG helper failed")
else
  RAG_OUTPUT="RAG helper not found — install/enable knowledge-rag to enable hub discovery"
fi

# 3) Compose summary
SUMMARY=$(cat <<EOF
# Airship Discovery — ${TIMESTAMP}

## Top hub & related docs
${RAG_OUTPUT}

## Market analysis
$(if [[ -x "$RESEARCH_SCRIPT" ]]; then echo "Executed: \`$RESEARCH_SCRIPT\`"; else echo "Skipped (no executable script)"; fi)

## Next steps
- Review the top hub above before planning work.
- Use related docs to inform implementation choices.
- Re-run this command anytime to refresh context.
EOF
)

if $SAVE; then
  OUT_FILE="${OUT_DIR}/${TIMESTAMP}.md"
  echo "$SUMMARY" > "$OUT_FILE"
  log "Saved discovery to $OUT_FILE"
  cat "$OUT_FILE"
else
  echo "$SUMMARY"
fi
```

### tools/rag_top_hub.py
```python
#!/usr/bin/env python3
"""
Lightweight helper to query the knowledge graph (Neo4j) for the most-connected hub
and return top related docs. Falls back to local search if Neo4j is unavailable.
"""
import os
import sys
import json
from datetime import datetime

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDriver = None

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

def query_top_hub_via_neo4j():
    if GraphDriver is None:
        return None, "neo4j driver not installed"
    try:
        driver = GraphDriver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            # Find node with highest degree (most connections)
            result = session.run("""
                MATCH (n)
                WITH n, size((n)--()) AS degree
                ORDER BY degree DESC
                LIMIT 1
                RETURN n.name AS name, labels(n) AS labels, degree
            """)
            record = result.single()
            if not record:
                return None, "no nodes found"
            name = record["name"]
            degree = record["degree"]

            # Top related docs (neighbors with :Doc or :File labels)
            related = session.run("""
                MATCH (n {name: $name})--(m)
                WHERE "Doc" IN labels(m) OR "File" IN labels(m) OR "Document" IN labels(m)
                RETURN m.name AS name, labels(m) AS labels
                ORDER BY m.name
                LIMIT 10
            """, name=name)
            related_docs = [r["name"] for r in related]
            driver.close()
            return {
                "hub": name,
                "degree": degree,
                "related_docs": related_docs,
                "source": "neo4j"
            }, None
    except Exception as e:
        return None, f"neo4j error: {e}"

def fallback_local_search():
    # Lightweight fallback: look for common hub-like filenames or MOC references
    candidates = []
    for root, _, files in os.walk("."):
        # skip hidden and build dirs
        if any(p.startswith(".") or p.startswith("node_modules") or p.startswith("venv") for p in root.split("/")):
            continue
        for f in files:
            if f.lower().endswith((".md", ".txt", ".yml", ".yaml", ".json")):
                candidates.append(os.path.join(root, f))
    # naive scoring: prefer MOC and short paths
    scored = []
    for c in candidates[:200]:
        score = 0
        name = os.path.basename(c).lower()
        if "moc" in name:
            score += 10
        if "hub" in name:
            score += 5
        if "readme" in name:
            score += 3
        score -= len(c) * 0.01
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    top = [c for _, c in scored[:5]]
    return {
        "hub": "MOC (fallback)",
        "degree": None,
        "related_docs": top,
        "source": "local-fallback"
    }, None

def main():
    data, err = query_top_hub_via_neo4j()
    if err or not data:
        data, err2 = fallback_local_search()
        if err2:
            print(f"ERROR: {err}; fallback failed: {err2}", file=sys.stderr)
            sys.exit(1)

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "hub": data.get("hub"),
        "degree": data.get("degree"),
        "related_docs": data.get("related_docs", []),
        "source": data.get("source")
    }

    # Human-friendly default output
    print(f"Top hub: {out['hub']} (degree: {out['degree'] or 'N/A'})")
    print("Related docs:")
    for d in out["related_docs"][:10]:
        print(f"  - {d}")
    print(f"\nSource: {out['source']}")

    # Also emit compact JSON on stdout if caller wants machine-readable (optional)
    # (kept simple: only human output by default)
    if os.getenv("AIRSHIP_DISCOVER_JSON"):
        print("\n---JSON---")
        print(json.dumps(out, indent=2))

if __name__ == "__main
