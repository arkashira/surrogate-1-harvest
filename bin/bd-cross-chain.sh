#!/usr/bin/env bash
# Cross-Project Chain Detector — scans specs/*.md + priority.json to find:
#   1. Producer specs (emit API surface, dataset, artifact)
#   2. Consumer specs (require/use that surface)
#   3. Emits a dependency chain to chains.json
#
# Also boosts consumer priority score once producer is committed (auto-unlock).
#
# Pattern matching (MVP):
#   Producer signals (in spec body):
#     - "emits", "produces", "exposes", "serves"
#     - "/v1/...", "GET /", "POST /"
#     - "unlock <project>"
#   Consumer signals:
#     - "consumes", "requires", "uses", "depends on"
#     - "via <other-project>"
#
# Run every 30 min.
set -u

LOG="$HOME/.claude/logs/bd-cross-chain.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
CHAINS="$SHARED/chains.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")"

"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import json, re
from pathlib import Path
from datetime import datetime

SHARED = Path.home() / '.hermes/workspace/swarm-shared'
SPECS_DIR = SHARED / 'specs'
PRI_PATH = SHARED / 'priority.json'
CHAINS_PATH = SHARED / 'chains.json'

# Known cross-project integration patterns (hand-seeded, extended by pattern match)
KNOWN_CHAINS = [
    # surrogate p40 serves LLM API → arkship consumes as backend
    {"producer": "p40", "consumer_pattern": r"arkship.*(llm|inference|copilot|suggest|generate)",
     "surface": "POST /v1/chat/completions (OpenAI-compat on vLLM)", "boost": 2},
    # surrogate p45 emits DPO dataset → arkship training signal
    {"producer": "p45", "consumer_pattern": r"arkship.*(train|finetune|preference)",
     "surface": "DPO dataset from lessons_learned", "boost": 1},
    # arkship p37 Kubecost ingest → Costinel p24 OpenCost enrich
    {"producer": "p37", "consumer_pattern": r"costinel.*(k8s|kubernetes|opencost|namespace)",
     "surface": "namespace_costs table", "boost": 1},
    # Vanguard p11 CIEM finding → Costinel IAM-cost attribution
    {"producer": "p11", "consumer_pattern": r"costinel.*(iam|unused|role|policy)",
     "surface": "Finding(source_engine='ciem') via shared schema", "boost": 1},
    # Vanguard p14 IOC match → arkship p34 runbook auto-execute trigger
    {"producer": "p14", "consumer_pattern": r"arkship.*(runbook|remediation|ioc|threat)",
     "surface": "IOC match alert → Temporal preset trigger", "boost": 2},
    # Vanguard p15 Cosign → arkship p32 Cosign gate (sibling — same binary)
    {"producer": "p15", "consumer_pattern": r"arkship.*(cosign|sbom|sigstore|attestation)",
     "surface": "cosign verify subprocess pattern (share Dockerfile install)", "boost": 1},
    # Costinel p20 FOCUS → arkship p37 cost integration
    {"producer": "p20", "consumer_pattern": r"arkship.*(focus|cost|tag.*compliance)",
     "surface": "FOCUS v1.2 row format (shared data contract)", "boost": 1},
    # Vanguard p13 Sigma rules → arkship p34 runbook trigger
    {"producer": "p13", "consumer_pattern": r"arkship.*(sigma|detection|alert.*runbook)",
     "surface": "Sigma rule match event → runbook lookup key", "boost": 1},
]

# Also discover via text pattern: look for "unlocks X" or "requires Y" in specs
def scan_spec_text(spec_path):
    """Return (produces, consumes) keyword sets for a spec."""
    try:
        txt = spec_path.read_text().lower()
    except Exception:
        return set(), set()
    produces = set()
    consumes = set()
    # Producer signals
    for m in re.finditer(r'(emits?|exposes?|produces?|serves?|unlocks?)\s+([a-z/][\w\s/.:-]{3,80})', txt):
        produces.add(m.group(2).strip()[:80])
    # Consumer signals
    for m in re.finditer(r'(consumes?|requires?|depends?\s+on|uses?\s+(output|api|service|endpoint))\s+(from\s+|of\s+)?([a-z/p][\w\s/.:-]{3,80})', txt):
        consumes.add(m.group(4).strip()[:80])
    return produces, consumes


# Load state
try:
    pri = json.loads(PRI_PATH.read_text())
except Exception:
    print("[chain] failed to load priority.json"); raise SystemExit

priorities_by_id = {p['id']: p for p in pri.get('priorities', [])}

# Build chain graph
chains = []

# 1. Apply known chains
for chain in KNOWN_CHAINS:
    prod_id = chain['producer']
    if prod_id not in priorities_by_id:
        continue
    for pid, p in priorities_by_id.items():
        if pid == prod_id:
            continue
        text = f"{p.get('project','').lower()} {p.get('title','').lower()} {p.get('description','').lower()}"
        if re.search(chain['consumer_pattern'], text):
            chains.append({
                "producer": prod_id,
                "producer_project": priorities_by_id[prod_id].get('project', '?'),
                "producer_status": priorities_by_id[prod_id].get('status', '?'),
                "consumer": pid,
                "consumer_project": p.get('project','?'),
                "consumer_status": p.get('status','?'),
                "surface": chain['surface'],
                "score_boost": chain['boost'],
                "source": "hand-curated",
            })

# 2. Pattern-mine from spec text (de-dup vs known)
existing_pairs = {(c['producer'], c['consumer']) for c in chains}

for spec_a in sorted(SPECS_DIR.glob('p*.md')):
    id_a = spec_a.stem
    if id_a not in priorities_by_id:
        continue
    prod_a, cons_a = scan_spec_text(spec_a)
    for spec_b in sorted(SPECS_DIR.glob('p*.md')):
        id_b = spec_b.stem
        if id_b == id_a: continue
        if id_b not in priorities_by_id: continue
        if (id_a, id_b) in existing_pairs: continue
        prod_b, cons_b = scan_spec_text(spec_b)
        # If spec_a produces something that spec_b consumes (fuzzy string overlap)
        for p_phrase in prod_a:
            for c_phrase in cons_b:
                # Require overlap ≥ 10 chars (non-trivial keyword shared)
                shared = set(p_phrase.split()) & set(c_phrase.split())
                meaningful = [w for w in shared if len(w) > 5 and w not in
                              ('service', 'system', 'policy', 'method', 'output', 'schema')]
                if len(meaningful) >= 1:
                    chains.append({
                        "producer": id_a,
                        "producer_project": priorities_by_id[id_a].get('project','?'),
                        "producer_status": priorities_by_id[id_a].get('status','?'),
                        "consumer": id_b,
                        "consumer_project": priorities_by_id[id_b].get('project','?'),
                        "consumer_status": priorities_by_id[id_b].get('status','?'),
                        "surface": f"pattern-match: {','.join(meaningful)}",
                        "score_boost": 0,
                        "source": "auto-discovered",
                    })
                    existing_pairs.add((id_a, id_b))
                    break
            if (id_a, id_b) in existing_pairs: break


# 3. For every chain where producer is DONE (merged/committed) and consumer still ready:
#    bump consumer priority score (so workers prioritize unlocked work)
cross_project_chains = [c for c in chains if c['producer_project'] != c['consumer_project']]

# Save chains file
chains_state = {
    "version": "1.0",
    "generated_at": datetime.utcnow().isoformat() + 'Z',
    "total_chains": len(chains),
    "cross_project_chains": len(cross_project_chains),
    "chains": chains,
}
CHAINS_PATH.write_text(json.dumps(chains_state, indent=2))

# Summary for log
print(f"[chain] total={len(chains)} cross_project={len(cross_project_chains)}")
# Top 5 chains by score-boost
for c in sorted(chains, key=lambda x: -x.get('score_boost', 0))[:5]:
    print(f"  {c['producer']}({c['producer_project']}) → {c['consumer']}({c['consumer_project']}): {c['surface'][:80]}")
PYEOF

echo "[$(date '+%H:%M:%S')] chains scan done" >> "$LOG"
