#!/usr/bin/env bash
# Query the graph DB — traversal, relationships, paths
# Usage:
#   graph-query.sh tag <name>              — docs with tag
#   graph-query.sh related <doc>           — docs connected (1-2 hops)
#   graph-query.sh path <a> <b>            — shortest path
#   graph-query.sh orphans                 — disconnected docs
#   graph-query.sh hubs                    — most-connected docs
#   graph-query.sh stats                   — graph overview
set -e
PYTHON="${HOME}/.claude/venv/bin/python"
[ -x "$PYTHON" ] || { echo "venv not found"; exit 1; }

CMD="${1:-stats}"
A1="${2:-}"
A2="${3:-}"

"$PYTHON" <<PY
from redislite.falkordb_client import FalkorDB
from pathlib import Path
db = FalkorDB(dbfilename=str(Path.home() / ".claude/graph-db.rdb"))
g = db.select_graph("ashira")

cmd, a1, a2 = "$CMD", "$A1", "$A2"
queries = {
    "tag":     f"MATCH (d:Doc)-[:TAGGED]->(:Tag {{name:'{a1}'}}) RETURN d.name, d.category, d.severity",
    "related": f"MATCH (a:Doc {{name:'{a1}'}})-[:LINKS_TO*1..2]-(b:Doc) RETURN DISTINCT b.name, b.category LIMIT 20",
    "path":    f"MATCH p = shortestPath((a:Doc {{name:'{a1}'}})-[:LINKS_TO*]-(b:Doc {{name:'{a2}'}})) RETURN [n IN nodes(p) | n.name]",
    "orphans": "MATCH (d:Doc) WHERE NOT (d)-[:LINKS_TO]-(:Doc) RETURN d.name, d.category LIMIT 30",
    "hubs":    "MATCH (d:Doc)-[r:LINKS_TO]-() RETURN d.name, count(r) AS links ORDER BY links DESC LIMIT 15",
    "stats":   "MATCH (n) RETURN labels(n)[0] AS type, count(*) AS c",
}
q = queries.get(cmd)
if not q:
    print(f"Unknown cmd. Available: {list(queries.keys())}"); exit(1)

r = g.query(q)
for row in r.result_set: print(row)
PY
