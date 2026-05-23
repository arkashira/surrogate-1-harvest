#!/usr/bin/env bash
# Orchestrator Flagger — scans specs to detect ones needing multi-domain split.
# When a spec touches code + infra + testing simultaneously → flag for orchestrator.
# Flagged specs route to a dedicated orchestrator worker (via env HERMES_USE_ORCHESTRATOR=1).
#
# Heuristics (spec text):
#   NEEDS OPS  — mentions: Dockerfile, helm, k8s, kubernetes, terraform, CI/CD, ArgoCD, Temporal preset,
#                admission webhook, systemd, launchd, nginx, load balancer
#   NEEDS QA   — mentions: integration test, e2e, property test, hypothesis, load test, performance
#   NEEDS DEV  — baseline — every spec has dev work
#
# Flag when: needs_ops=True AND (needs_qa=True OR multi-file >=3) → orchestrator
#
# Also emits recommendations to `~/.hermes/workspace/swarm-shared/orchestrator-recommendations.json`.
set -u

LOG="$HOME/.claude/logs/bd-orchestrator-flagger.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")"

"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import json, re
from pathlib import Path
from datetime import datetime

SHARED = Path.home() / '.hermes/workspace/swarm-shared'
SPECS_DIR = SHARED / 'specs'
PRI_PATH = SHARED / 'priority.json'
OUT_PATH = SHARED / 'orchestrator-recommendations.json'

# Ops/infra signals — spec touches deployment/infra
OPS_KEYWORDS = [
    'dockerfile', 'helm chart', 'helm/', 'k8s', 'kubernetes', 'admission webhook',
    'terraform', 'cloudformation', 'argocd', 'fluxcd', 'temporal preset', 'nginx',
    'load balancer', 'cert-manager', 'ingress', 'launchd', 'systemd', 'cron',
    'daemonset', 'statefulset', 'deployment.yaml', 'service mesh', 'iam policy',
    'rbac manifest', 'secret rotation', 'runbook', 'vault', 'sops', 'hashicorp',
]

QA_KEYWORDS = [
    'integration test', 'e2e test', 'property test', 'hypothesis', 'property-based',
    'load test', 'performance test', 'contract test', 'snapshot test', 'fuzz test',
    'chaos test', 'canary deploy',
]


def scan(spec_path):
    try:
        txt = spec_path.read_text().lower()
    except Exception:
        return {"error": "unreadable"}

    ops_hits = [kw for kw in OPS_KEYWORDS if kw in txt]
    qa_hits = [kw for kw in QA_KEYWORDS if kw in txt]

    # Count file-creations in spec (### File: ... new) as proxy for complexity
    file_mentions = re.findall(r'###\s*file:\s*([\w/.-]+)', txt)
    file_count = len(set(file_mentions))

    needs_ops = len(ops_hits) >= 1
    needs_qa = len(qa_hits) >= 1

    # Orchestrator needed when: ops + (qa OR complex)
    needs_orchestrator = needs_ops and (needs_qa or file_count >= 3)

    return {
        "needs_ops": needs_ops,
        "needs_qa": needs_qa,
        "file_count": file_count,
        "needs_orchestrator": needs_orchestrator,
        "ops_signals": ops_hits[:3],
        "qa_signals": qa_hits[:3],
    }


try:
    pri = json.loads(PRI_PATH.read_text())
except Exception:
    print("[flagger] priority.json load fail"); raise SystemExit

recommendations = []
by_id = {p['id']: p for p in pri.get('priorities', [])}

for spec_path in sorted(SPECS_DIR.glob('p*.md')):
    pid = spec_path.stem
    if pid not in by_id:
        continue
    if by_id[pid].get('status') != 'ready':
        continue
    analysis = scan(spec_path)
    if analysis.get('needs_orchestrator'):
        recommendations.append({
            "priority_id": pid,
            "project": by_id[pid].get('project','?'),
            "title": by_id[pid].get('title','?'),
            **analysis,
        })

state = {
    "version": "1.0",
    "generated_at": datetime.utcnow().isoformat() + 'Z',
    "total_flagged": len(recommendations),
    "recommendations": recommendations,
}
OUT_PATH.write_text(json.dumps(state, indent=2))

print(f"[flagger] flagged={len(recommendations)} priorities needing orchestrator")
for r in recommendations[:8]:
    print(f"  {r['priority_id']:<5} [{r['project']:<10}] ops={r['ops_signals'][:2]} qa={r['qa_signals'][:2]} files={r['file_count']}")
PYEOF

echo "[$(date '+%H:%M:%S')] flagger done" >> "$LOG"
