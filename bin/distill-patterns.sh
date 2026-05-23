#!/usr/bin/env bash
# Self-training pipeline — extract patterns from interaction log
# Reads ~/.claude/interactions/*.jsonl → uses best available AI to find novel patterns
# → writes new pattern files if found → updates knowledge_index + graph DB
#
# Run: weekly or after significant interaction volume
# Usage:
#   distill-patterns.sh                    # analyze last 7 days
#   distill-patterns.sh --since 2026-04-01 # custom range
#   distill-patterns.sh --preview          # dry-run, print proposals only
set -e

SINCE=""
PREVIEW=0

while [ $# -gt 0 ]; do
  case "$1" in
    --since)   SINCE="$2"; shift 2 ;;
    --preview) PREVIEW=1; shift ;;
    *) shift ;;
  esac
done

# Default: last 7 days
[ -z "$SINCE" ] && SINCE=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)

LOG_DIR="$HOME/.claude/interactions"
[ ! -d "$LOG_DIR" ] && { echo "No interaction log yet"; exit 0; }

# Aggregate recent entries
AGG_FILE=$(/usr/bin/mktemp)
for f in "$LOG_DIR"/*.jsonl; do
  [ -f "$f" ] && "$HOME/.claude/venv/bin/python" -c "
import json, sys
from datetime import datetime
since = datetime.fromisoformat('$SINCE')
with open('$f') as fp:
    for line in fp:
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e['ts'][:19]) >= since:
                print(json.dumps({'q':e['query'][:200],'r':e['response'][:500],'model':e['model']}))
        except: pass
" >> "$AGG_FILE"
done

COUNT=$(/usr/bin/wc -l < "$AGG_FILE" | /usr/bin/tr -d ' ')
echo "Analyzing $COUNT interactions since $SINCE..."
[ "$COUNT" -eq 0 ] && { rm "$AGG_FILE"; exit 0; }

# Use ai-fallback (will use cheapest available) to extract patterns
PROMPT=$(cat <<EOF
Analyze these AI interaction logs. Find NOVEL recurring patterns worth documenting.
Return ONLY a JSON array of objects: [{"pattern_name":"kebab-case","category":"auth|process|security|skills|engineering","symptom":"...","root_cause":"...","fix":"...","tags":["tag1","tag2"]}]
If nothing novel, return [].

Log sample:
$(/usr/bin/head -50 "$AGG_FILE")
EOF
)

# Pattern extraction = reasoning task → auto-routes to DeepSeek-R1 / Grok 3 / R1 distill
PATTERNS=$("/opt/surrogate-1-harvest/bin/ai-fallback.sh" --task reasoning "$PROMPT" 2>/dev/null || echo "[]")

if [ "$PREVIEW" = "1" ]; then
  echo "=== Proposed patterns ==="
  echo "$PATTERNS"
  rm "$AGG_FILE"
  exit 0
fi

# Parse + write pattern files
"$HOME/.claude/venv/bin/python" <<PY
import json, os, re
from pathlib import Path
from datetime import date

raw = """$PATTERNS"""
# extract JSON array from possibly-prefixed response
m = re.search(r'\[.*\]', raw, re.DOTALL)
if not m:
    print("No patterns extracted"); exit(0)
try:
    patterns = json.loads(m.group(0))
except: exit(0)

if not patterns:
    print("No novel patterns found"); exit(0)

base = Path.home() / "Documents/Obsidian Vault/AI-Hub/patterns"
written = 0
for p in patterns:
    name = p.get("pattern_name", "").strip()
    cat = p.get("category", "process").strip()
    if not name: continue
    dest = base / cat / f"{name}.md"
    if dest.exists(): continue  # skip existing
    dest.parent.mkdir(parents=True, exist_ok=True)
    tags = p.get("tags", []) + [cat, "auto-distilled"]
    body = f"""---
pattern: {name}
tags: {json.dumps(tags)}
first_seen: {date.today().isoformat()}
last_seen: {date.today().isoformat()}
severity: medium
source: auto-distilled-from-interactions
---

# {name.replace('-', ' ').title()}

## Symptom
{p.get('symptom', '(auto-extracted, review)')}

## Root Cause
{p.get('root_cause', '(auto-extracted, review)')}

## Fix
{p.get('fix', '(auto-extracted, review)')}

## Prevention
(review + fill in)

## See Also
- [[../MOC|Knowledge Graph Hub]]
"""
    dest.write_text(body)
    written += 1
    print(f"  ✅ new pattern: {cat}/{name}.md")

print(f"\\n{written} new patterns written")
PY

# Re-sync graph if anything written
[ -x "/opt/surrogate-1-harvest/bin/graph-sync.sh" ] && "/opt/surrogate-1-harvest/bin/graph-sync.sh" > /dev/null 2>&1

rm "$AGG_FILE"
