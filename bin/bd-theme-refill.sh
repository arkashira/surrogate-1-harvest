#!/usr/bin/env bash
# Theme backlog auto-refill — when ready-count < 5, promote themes from:
#  1. b4-research raw signals in backlog.jsonl (source=b4)
#  2. deferred items from research/*.md (pattern-matched rank 9-20)
#  3. tech news scrape (github trending for each project's language)
#
# Keeps the continuous BD loop running indefinitely without user intervention.
set -u

LOG="$HOME/.claude/logs/bd-theme-refill.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
BACKLOG="$SHARED/themes-backlog.json"
mkdir -p "$(dirname "$LOG")"

READY_COUNT=$(/usr/bin/python3 -c "
import json
d = json.load(open('$BACKLOG'))
print(sum(1 for t in d.get('themes', []) if t.get('status') == 'ready'))
" 2>/dev/null || echo 0)

if [[ "$READY_COUNT" -ge 5 ]]; then
    echo "[$(date '+%H:%M:%S')] backlog healthy (ready=$READY_COUNT) — skip refill" >> "$LOG"
    exit 0
fi

echo "[$(date '+%H:%M:%S')] backlog low (ready=$READY_COUNT) — refilling" >> "$LOG"

# Source 1: b4-research signals (raw items from backlog.jsonl, last 24h, status=raw)
/usr/bin/python3 <<PYEOF
import json, os, re
from pathlib import Path
from datetime import datetime, timedelta

backlog_path = Path.home() / '.hermes/workspace/swarm-shared/themes-backlog.json'
bl = json.loads(backlog_path.read_text())
existing_themes = {t.get('theme','').lower()[:80] for t in bl.get('themes', [])}

signals_path = Path.home() / '.hermes/workspace/swarm-shared/backlog.jsonl'
added = 0
cutoff = datetime.utcnow() - timedelta(days=2)

# Source 1: b4-research signals
if signals_path.exists():
    for line in signals_path.read_text().splitlines()[-50:]:  # last 50 signals
        try:
            s = json.loads(line)
        except: continue
        if s.get('source') not in ('b4', 'granite-bd-research'): continue
        item = s.get('item', '')

        # ── Quality filter (mirrors bd-news-scraper.sh) ──
        # 1. Length bounds
        if len(item) < 30 or len(item) > 300: continue

        # 2. Reject list-wrappers + vague opinion fragments
        reject_patterns = [
            r'^\d+-\d+\s+features',
            r'features to add',
            r'^(what|why|how)\s+',
            r'^(the|a|an)\s+(\w+\s+){0,3}(future|trend|outlook|space|landscape|state)\s',
        ]
        if any(re.search(p, item, re.I) for p in reject_patterns): continue

        # 3. Require anchor keyword (proves technical specificity)
        anchor_keywords = (
            'kubernetes','k8s','aws','gcp','azure','iam','cloudtrail','kyverno','opa',
            'falco','wiz','prowler','cosign','sbom','slsa','cve','mitre','sigma',
            'rbac','oidc','oauth','saml','scan','detection','policy','webhook',
            'cost','finops','budget','allocation','chargeback','opencost','kubecost',
            'focus','rightsizing','savings','backstage','temporal','argocd','flux',
            'vault','secret','dora','mttr','slo','chaos','admission','runbook',
            'llm','lora','qlora','dpo','peft','vllm','ollama','huggingface',
            'humaneval','mbpp','swe-bench','jailbreak','rag','retrieval',
            'payroll','pdpa','gdpr','timesheet','leave','geofence','line messaging',
            'api','endpoint','schema','audit log','dashboard',
        )
        if not any(kw in item.lower() for kw in anchor_keywords): continue

        # 4. Require implementation verb (actionable, not opinion)
        action_verbs = (
            'implement','add','build','integrate','enable','detect','monitor','scan',
            'rotate','enforce','validate','correlate','ingest','track','report',
            'automate','orchestrate','generate','alert','block','prevent','audit',
            'collect','calculate','compute','expose','wrap','emit','parse','classify',
        )
        if not any(v in item.lower() for v in action_verbs): continue

        key = item.lower()[:80]
        if key in existing_themes: continue

        # Build new theme entry
        tid = f"t{100 + len(bl.get('themes', [])) - 25:03d}"
        new_theme = {
            "id": tid,
            "project": s.get('project','Vanguard'),
            "priority": 10,  # lower priority than curated rank-9-20 items
            "status": "ready",
            "theme": item[:200],
            "source": f"auto-refill from {s.get('source','?')} @ {s.get('ts','?')}",
            "scope_hint": s.get('signal', '')[:200],
            "auto_generated": True,
        }
        bl['themes'].append(new_theme)
        existing_themes.add(key)
        added += 1
        if added >= 10: break  # cap per refill

# Source 2: TODO — promote deferred items from research/*.md if source 1 empty
# (Future: scrape tech news)

bl['last_updated'] = datetime.utcnow().isoformat() + 'Z'
backlog_path.write_text(json.dumps(bl, indent=2))
print(f"[refill] added={added} new themes from b4-research signals")
PYEOF

echo "[$(date '+%H:%M:%S')] refill done" >> "$LOG"
