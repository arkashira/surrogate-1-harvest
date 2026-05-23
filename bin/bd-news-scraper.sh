#!/usr/bin/env bash
# Tech-news scraper — pulls signals from RSS feeds, classifies by project,
# pushes to themes-backlog.json as low-priority auto-themes.
#
# Feeds per project (2026 working endpoints):
#   Vanguard   → HN security, The New Stack "cloud-security", thehackernews
#   Costinel   → HN finops, Product Hunt cloud-cost, The New Stack "cloud-cost"
#   arkship    → HN devops, The New Stack "platform engineering", Product Hunt developer-tools
#   surrogate  → HN ai, huggingface blog rss, ML reddit RSS
#   workio     → HN hr, techcrunch-thailand, daily-news HR topics
#
# Quality filter:
#   - title length 20-200 chars
#   - contains a concrete noun (product name / API / standard / tech)
#   - not pure opinion piece ("Why X is..." titles rejected)
#   - de-duped against existing themes
#
# Run every 6h. Adds up to 15 auto-themes per run, priority=20 (lower than curated).
set -u

LOG="$HOME/.claude/logs/bd-news-scraper.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
BACKLOG="$SHARED/themes-backlog.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")"

"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import feedparser
import json
import re
from pathlib import Path
from datetime import datetime
from urllib.request import Request, urlopen

BACKLOG = Path.home() / '.hermes/workspace/swarm-shared/themes-backlog.json'

# Feed → default-project mapping. Each feed has a keyword filter too.
FEEDS = [
    # Vanguard — cloud security / CSPM / CNAPP
    {'url': 'https://hnrss.org/newest?q=CSPM+OR+CNAPP+OR+%22cloud+security%22', 'project': 'Vanguard', 'priority': 20},
    {'url': 'https://hnrss.org/newest?q=%22IAM%22+OR+%22Access+Analyzer%22+OR+%22Sigma+rule%22', 'project': 'Vanguard', 'priority': 20},
    {'url': 'https://thenewstack.io/category/security/feed/', 'project': 'Vanguard', 'priority': 22},

    # Costinel — FinOps / cloud cost
    {'url': 'https://hnrss.org/newest?q=FinOps+OR+%22cloud+cost%22+OR+OpenCost', 'project': 'Costinel', 'priority': 20},
    {'url': 'https://hnrss.org/newest?q=%22cost+anomaly%22+OR+%22savings+plan%22+OR+%22rightsizing%22', 'project': 'Costinel', 'priority': 20},

    # arkship — platform engineering / DevSecOps
    {'url': 'https://hnrss.org/newest?q=%22platform+engineering%22+OR+Backstage+OR+%22golden+path%22', 'project': 'arkship', 'priority': 20},
    {'url': 'https://hnrss.org/newest?q=DevSecOps+OR+%22supply+chain%22+OR+Sigstore+OR+SLSA', 'project': 'arkship', 'priority': 20},
    {'url': 'https://thenewstack.io/category/platform-engineering/feed/', 'project': 'arkship', 'priority': 22},

    # surrogate — AI training / model eval
    {'url': 'https://hnrss.org/newest?q=%22fine-tune%22+OR+QLoRA+OR+%22DPO%22+OR+%22SWE-bench%22', 'project': 'surrogate', 'priority': 20},
    {'url': 'https://hnrss.org/newest?q=%22lm-eval%22+OR+%22contamination%22+OR+%22Axolotl%22+OR+%22vLLM%22', 'project': 'surrogate', 'priority': 20},

    # workio — HR / frontline / LINE (Thai)
    {'url': 'https://hnrss.org/newest?q=%22payroll%22+OR+%22time+tracking%22+OR+PDPA', 'project': 'workio', 'priority': 25},
]

# Reject titles matching any of these (low-signal content)
REJECT_PATTERNS = [
    r'^ask hn:?\s',
    r'^show hn:?\s*$',        # Show HN titles sometimes just "Show HN:"
    r'^why\s+(we|i)\s',        # opinion pieces
    r'hiring|who is hiring',
    r'^\d+\s+(reasons?|ways?)\s+',   # listicles
    r'monthly|weekly|digest|roundup',
    r'^the\s+\w+\s+is\s+(dead|broken|bad)',
    r'^my\s+.{0,20}(journey|story|experience)',
]

# Must contain at least one of these (technical anchors — proof it's implementation-relevant)
ANCHOR_KEYWORDS = [
    # Security
    'kubernetes', 'k8s', 'aws', 'gcp', 'azure', 'eks', 'ecs', 'iam', 'cloudtrail', 's3', 'ec2', 'lambda',
    'kyverno', 'opa', 'falco', 'wiz', 'prowler', 'cosign', 'sbom', 'slsa', 'cve', 'mitre', 'sigma',
    'rbac', 'abac', 'oidc', 'oauth', 'saml', 'mfa', 'sso',
    # FinOps
    'cost', 'finops', 'spend', 'budget', 'allocation', 'chargeback', 'showback', 'reservation',
    'opencost', 'kubecost', 'focus', 'anomaly', 'rightsizing', 'savings plan', 'unit econ',
    # Platform engineering
    'backstage', 'port', 'humanitec', 'temporal', 'argo', 'flux', 'vault', 'secret', 'rotation',
    'dora', 'mttr', 'slo', 'sli', 'chaos', 'admission', 'policy', 'runbook',
    # ML
    'llm', 'lora', 'qlora', 'dpo', 'rlhf', 'peft', 'vllm', 'ollama', 'huggingface', 'transformers',
    'humaneval', 'mbpp', 'swe-bench', 'mt-bench', 'jailbreak', 'prompt injection',
    # HR
    'payroll', 'pdpa', 'gdpr', 'timesheet', 'leave', 'geofence', 'line messaging', 'hrms',
]


def http_get(url, timeout=10):
    """Fetch with UA (some feeds block default python-urllib)."""
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 bd-news-scraper/1.0'})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        return None


def is_quality_title(title: str) -> bool:
    """Apply reject filter + anchor keyword requirement."""
    if not title:
        return False
    t = title.strip()
    if len(t) < 20 or len(t) > 200:
        return False
    tl = t.lower()
    for pat in REJECT_PATTERNS:
        if re.search(pat, tl, re.I):
            return False
    if not any(kw in tl for kw in ANCHOR_KEYWORDS):
        return False
    return True


def extract_scope_hint(entry) -> str:
    """Extract usable scope hint: summary (stripped HTML), or first sentence."""
    summary = ''
    for k in ('summary', 'description'):
        if entry.get(k):
            summary = re.sub(r'<[^>]+>', '', entry[k])
            break
    summary = summary.strip()[:250]
    if entry.get('link'):
        summary = f"{summary} | signal from {entry['link']}"
    return summary[:400]


# Load existing backlog
try:
    bl = json.loads(BACKLOG.read_text())
except Exception:
    print(f"[news] failed to load backlog — aborting")
    raise SystemExit

existing_keys = {t.get('theme','').lower()[:80] for t in bl.get('themes', [])}
existing_urls = {t.get('url','') for t in bl.get('themes', []) if t.get('url')}

added = 0
cap_per_run = 15

for feed_cfg in FEEDS:
    if added >= cap_per_run:
        break
    raw = http_get(feed_cfg['url'])
    if not raw:
        continue
    try:
        parsed = feedparser.parse(raw)
    except Exception:
        continue

    for entry in parsed.entries[:8]:  # max 8 per feed
        if added >= cap_per_run:
            break
        title = (entry.get('title') or '').strip()
        link = (entry.get('link') or '').strip()
        if not is_quality_title(title):
            continue
        key = title.lower()[:80]
        if key in existing_keys or link in existing_urls:
            continue

        # Build theme entry
        tid = f"tn{1000 + added + sum(1 for t in bl.get('themes', []) if t.get('id','').startswith('tn')):04d}"
        new_theme = {
            "id": tid,
            "project": feed_cfg['project'],
            "priority": feed_cfg['priority'],
            "status": "ready",
            "theme": title,
            "source": f"news-scraper | feed={feed_cfg['url'][:50]}",
            "scope_hint": extract_scope_hint(entry),
            "url": link,
            "auto_generated": True,
            "signal_type": "tech-news",
        }
        bl['themes'].append(new_theme)
        existing_keys.add(key)
        existing_urls.add(link)
        added += 1

bl['last_updated'] = datetime.utcnow().isoformat() + 'Z'
BACKLOG.write_text(json.dumps(bl, indent=2))
print(f"[news-scraper] added={added} themes (cap={cap_per_run}); backlog total={len(bl['themes'])}")
PYEOF

echo "[$(date '+%H:%M:%S')] news scrape done" >> "$LOG"
