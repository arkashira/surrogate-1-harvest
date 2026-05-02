# airship / discovery

## Highest-value incremental improvement
Add a lightweight discovery CLI (`airship discover`) that surfaces the most-connected hub (e.g., MOC) and top related docs before planning/execution — zero-config, <30s runtime, no infra changes.

## Implementation plan (<2h)
1. Add CLI entrypoint: `airship/discover.py` with `#!/usr/bin/env bash` shebang and `chmod +x`.
2. Implement fast graph query:
   - Use Neo4j bolt from env (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`) with fallback to local file or embedded Cypher.
   - Query top hub by degree centrality and return top-N related docs.
3. Output concise markdown table + JSON option for scripts.
4. Wire into `airship/cli.py` or shell wrapper so `airship discover` works immediately.
5. Add short help and examples in README snippet.

## Code snippets

### airship/discover.py
```python
#!/usr/bin/env python3
"""
airship discover
Surface the most-connected hub and related docs for contextual planning.
Usage:
  airship discover [--limit=N] [--format=json|table]
"""

import os
import json
import argparse
from datetime import datetime

try:
    from neo4j import GraphDatabase
    NEO4J_AVAILABLE = True
except Exception:
    NEO4J_AVAILABLE = False

DEFAULT_LIMIT = 5

def build_query(limit: int):
    # Find top hub by total degree (in+out), then related docs by weight
    return """
    MATCH (h:Hub)
    WITH h, size((h)--()) AS deg
    ORDER BY deg DESC
    LIMIT 1
    MATCH (h)-[r:RELATED_TO]-(doc:Doc)
    RETURN h.name AS hub,
           deg AS hub_degree,
           collect({doc: doc.name, weight: r.weight, kind: doc.kind})[..$limit] AS related
    """

def query_neo4j(uri, user, password, limit):
    if not NEO4J_AVAILABLE:
        raise RuntimeError("neo4j driver not available; install neo4j or use --format=table with fallback")
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session() as session:
            result = session.run(build_query(limit), limit=limit)
            return result.single()

def safe_query(limit):
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "neo4j")
    try:
        record = query_neo4j(uri, user, password, limit)
        if record:
            return {
                "hub": record["hub"],
                "hub_degree": record["hub_degree"],
                "related": record["related"],
                "source": "neo4j",
                "ts": datetime.utcnow().isoformat() + "Z"
            }
    except Exception as e:
        # Fallback: read local MOC insight if present
        fallback_path = os.path.join(os.path.dirname(__file__), "data", "top-hub-insight.json")
        try:
            with open(fallback_path) as f:
                fb = json.load(f)
                return {"hub": fb.get("hub", "MOC"), "hub_degree": fb.get("degree", 0),
                        "related": fb.get("related", []), "source": "fallback", "ts": datetime.utcnow().isoformat() + "Z"}
        except Exception:
            return {"error": str(e), "hint": "Set NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD or provide fallback data"}

def format_table(data, limit):
    lines = []
    lines.append(f"# Top hub (by degree)")
    lines.append(f"Hub: {data['hub']} (degree={data['hub_degree']})")
    lines.append("")
    lines.append(f"# Top {limit} related docs")
    lines.append("| Doc | Weight | Kind |")
    lines.append("|-----|--------|------|")
    for r in data.get("related", [])[:limit]:
        lines.append(f"| {r.get('doc', r.get('name', '?'))} | {r.get('weight', 1)} | {r.get('kind', '')} |")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="airship discover — surface top hub and related docs")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="number of related docs to show")
    parser.add_argument("--format", choices=["json", "table"], default="table", help="output format")
    args = parser.parse_args()

    data = safe_query(args.limit)
    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        print(f"Hint: {data.get('hint', '')}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(format_table(data, args.limit))
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
```

### airship/cli.py (or shell wrapper)
If you prefer a shell-first approach (per patterns), add:

```bash
#!/usr/bin/env bash
# airship/discover.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/discover.py" "$@"
```

Then make executable:
```bash
chmod +x airship/discover.py airship/discover.sh
```

And optionally link into top-level `airship` CLI or Makefile.

## Quick verification
```bash
# With Neo4j running
NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=neo4j ./airship/discover.py --limit=5

# Fallback (no Neo4j)
./airship/discover.py --format=json
```

## Next steps (optional, post-ship)
- Add caching (TTL 5m) to avoid repeated bolt queries during rapid iterations.
- Add `--hub=MOC` flag to force specific hub.
- Integrate into pre-task checklist in README.
