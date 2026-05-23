#!/usr/bin/env bash
# Scraped-to-Surrogate — convert scraper + research outputs into Surrogate-1
# training pairs. Runs every 2h. Appends to training-jsonl/scraped-YYYY-MM-DD.jsonl.
#
# Sources ingested:
#   1. backlog.jsonl (b4-research, granite-bd, granite-bd-research, granite-growth, etc.)
#   2. trends-2026/*.md (scrape-eng-blogs, scrape-hn-discussions rollups)
#   3. research/*.md (architect BD agents — Vanguard/Costinel/arkship/surrogate/workio/sota)
#   4. scraped-dev-patterns/*.md
#   5. code-style-scraped/*.md
#   6. Obsidian AI-Hub/patterns/*.md
#
# Format per line: {instruction, response, source, tags, timestamp}
# Dedup by sha1(instruction[:200]) to avoid re-ingestion.
set -u

LOG="$HOME/.claude/logs/scraped-to-surrogate.log"
OUT_DIR="$HOME/axentx/surrogate/data/training-jsonl"
OUT_FILE="$OUT_DIR/scraped-$(date +%Y-%m-%d).jsonl"
STATE="$HOME/.claude/state/scraped-to-surrogate.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR" "$(dirname "$STATE")"
[[ ! -f "$STATE" ]] && echo '{}' > "$STATE"

"$VENV_PY" <<PYEOF 2>>"$LOG"
import json, hashlib, re
from pathlib import Path
from datetime import datetime

HOME = Path.home()
OUT = Path("$OUT_FILE")
STATE = Path("$STATE")

# Load dedup state (hashes seen)
try: seen = set(json.loads(STATE.read_text()).get('seen', []))
except: seen = set()

def h(text):
    return hashlib.sha1((text or '')[:200].encode()).hexdigest()[:16]

# PRIVACY filter — drop any pair whose instruction or response references
# ~/develope/ paths or known client identifiers / RDS endpoints / known
# leaked credential patterns. Audit @ 2026-05-03 found 3 hits in
# scraped-2026-04-23.jsonl that came from workspace-map.md before
# redaction. This filter is the last gate before ingestion.
_DEVELOPE_PAT = re.compile(
    r"/Users/Ashira/develope|"
    r"~/develope|"
    r"develope/(?:Excise|Wine|Car|MFA|RD|nginx|Winmed|sherlock|"
    r"thinkbit-devops|hermes-toolbelt|surrogate-1-train|QA|AI/tb-)|"
    r"cj86ma6gsgzd|"      # known thinkbit RDS host suffix
    r"gpLu6ZbpINepfzh7GM",  # known leaked RDS password (rotate!)
    re.IGNORECASE,
)


def _is_private(*texts):
    return any(_DEVELOPE_PAT.search(t or "") for t in texts)


def write_pair(instr, resp, source, tags=None, extra=None):
    """Write one training pair if not seen; return 1 if written, 0 if skipped.
    Drops pairs that contain ~/develope refs or known client credentials."""
    if _is_private(instr, resp):
        return 0
    hh = h(instr)
    if hh in seen: return 0
    seen.add(hh)
    pair = {
        "instruction": (instr or '')[:3000],
        "response": (resp or '')[:5000],
        "source": source,
        "tags": tags or [],
        "timestamp": datetime.utcnow().isoformat() + 'Z',
    }
    if extra: pair.update(extra)
    with open(OUT, 'a') as f:
        f.write(json.dumps(pair, ensure_ascii=False) + "\n")
    return 1

counts = {}

# ── 1. backlog.jsonl (b4-research + granite-* output signals) ──
backlog = HOME / '.hermes/workspace/swarm-shared/backlog.jsonl'
if backlog.exists():
    n = 0
    for line in backlog.read_text().splitlines():
        if not line.strip(): continue
        try: d = json.loads(line)
        except: continue
        src = d.get('source', '')
        item = d.get('item', '')
        signal = d.get('signal', '')
        project = d.get('project', '')
        if len(item) < 30: continue
        # Frame as Q&A
        instr = f"What's a priority feature for {project} ({src}-identified)?"
        resp = f"{item}\n\nSignal source: {signal}"
        n += write_pair(instr, resp, f"scraped:{src}", tags=['backlog', project.lower()])
    counts['backlog'] = n

# ── 2. trends-2026 / knowledge rollups ──
for kb_dir in [
    HOME / 'Documents/Obsidian Vault/AI-Hub/knowledge/trends-2026',
    HOME / 'Documents/Obsidian Vault/AI-Hub/knowledge',
    HOME / 'Documents/Obsidian Vault/AI-Hub/patterns',
]:
    if not kb_dir.exists(): continue
    n = 0
    for md in kb_dir.glob('*.md'):
        try: text = md.read_text()
        except: continue
        # Each H2 section becomes an instruction/response pair
        # Q = section heading, A = section body
        sections = re.split(r'\n##\s+', text)
        for sec in sections[1:]:  # skip preamble
            first_nl = sec.find('\n')
            if first_nl < 0: continue
            heading = sec[:first_nl].strip()
            body = sec[first_nl:].strip()[:3000]
            if len(body) < 100: continue
            instr = f"[{md.stem}] {heading}"
            n += write_pair(instr, body, f"scraped:knowledge/{kb_dir.name}",
                            tags=[md.stem, kb_dir.name])
    counts[kb_dir.name] = n

# ── 3. research docs from architect BD agents ──
research = HOME / '.hermes/workspace/swarm-shared/research'
if research.exists():
    n = 0
    for md in research.glob('*.md'):
        try: text = md.read_text()
        except: continue
        sections = re.split(r'\n##\s+', text)
        for sec in sections[1:]:
            first_nl = sec.find('\n')
            if first_nl < 0: continue
            heading = sec[:first_nl].strip()
            body = sec[first_nl:].strip()[:3000]
            if len(body) < 150: continue
            instr = f"[research: {md.stem}] {heading}"
            n += write_pair(instr, body, 'scraped:research', tags=[md.stem])
    counts['research'] = n

# ── 4. specs — instruction=priority title, response=spec body ──
specs = HOME / '.hermes/workspace/swarm-shared/specs'
if specs.exists():
    n = 0
    for md in specs.glob('p*.md'):
        try: text = md.read_text()
        except: continue
        # First line is "# p1: title", use as instruction
        lines = text.split('\n', 1)
        if len(lines) < 2: continue
        heading = lines[0].lstrip('#').strip()
        body = lines[1][:4000]
        if len(body) < 200: continue
        n += write_pair(f"Implement: {heading}", body, 'scraped:spec',
                        tags=['spec', md.stem])
    counts['specs'] = n

# ── 5. decisions/BRDs from B1A1 agent ──
backlog_dir = HOME / '.hermes/workspace/swarm-shared/backlog'
if backlog_dir.exists():
    n = 0
    for md in backlog_dir.glob('*.md'):
        try: text = md.read_text()
        except: continue
        # First few lines are title + meta
        lines = text.split('\n', 4)
        if len(lines) < 2: continue
        title = lines[0].lstrip('#').strip()
        body = '\n'.join(lines[1:])[:4000]
        if len(body) < 150: continue
        n += write_pair(f"BRD: {title}", body, 'scraped:brd', tags=['brd', md.stem])
    counts['brds'] = n

# ── 6. decisions/20260423_... (user decision logs) ──
decisions = HOME / '.hermes/workspace/swarm-shared/decisions'
if decisions.exists():
    n = 0
    for md in decisions.glob('*.md'):
        try: text = md.read_text()
        except: continue
        lines = text.split('\n', 2)
        if len(lines) < 2: continue
        n += write_pair(
            f"Decision: {lines[0].lstrip('#').strip() or md.stem}",
            text[:3000], 'scraped:decision', tags=['decision', md.stem]
        )
    counts['decisions'] = n

# ── 7. index.db — THE BIG ONE: 98k+ scraped docs from 40+ sources ──
# Sources: code, github-public, rss, reddit, arxiv, domain:*, mythos-*, eng-blog:*,
# code-style, hf-papers, stackoverflow, github-trending, adr-wild, trending-rt, etc.
import sqlite3
INDEX_DB = HOME / '.claude/index.db'
if INDEX_DB.exists():
    conn = sqlite3.connect(str(INDEX_DB))
    cur = conn.cursor()
    # Only pull rows newer than last ingestion (track last ts per run)
    last_idx_ts = __import__('json').loads(STATE.read_text()).get('last_index_ts', '') if STATE.exists() else ''
    n = 0
    # Cap at 5000 rows per run (avoid huge jsonl explosions; enough for catch-up)
    query = """
        SELECT source, instruction, response, ts
        FROM docs
        WHERE length(instruction) > 20
          AND length(response) > 50
          AND ts > ?
        ORDER BY ts DESC
        LIMIT 5000
    """
    max_ts = last_idx_ts
    for row in cur.execute(query, (last_idx_ts,)):
        src, instr, resp, ts = row
        if ts > max_ts: max_ts = ts
        # Skip obvious low-quality
        if not instr or not resp: continue
        if len(resp) < 50: continue
        n += write_pair(instr, resp[:4000], f"scraped:{src}",
                        tags=[src.split(':',1)[0] if ':' in src else src])
    conn.close()
    counts['index.db'] = n
    # Persist max_ts so next run only pulls new rows
    state_obj = json.loads(STATE.read_text()) if STATE.exists() else {}
    state_obj['last_index_ts'] = max_ts
    state_obj['seen'] = list(seen)[:100000]  # cap state, 100k entries
    STATE.write_text(json.dumps(state_obj))

# ── 8. code-style scraped (from Obsidian patterns if exists) ──
code_style_dir = HOME / 'Documents/Obsidian Vault/AI-Hub/skills'
if code_style_dir.exists():
    n = 0
    for md in list(code_style_dir.rglob('SKILL.md'))[:80]:  # cap 80
        try: text = md.read_text()
        except: continue
        name = md.parent.name
        # First paragraph is description
        parts = text.split('\n\n', 3)
        if len(parts) < 2: continue
        desc = parts[0]
        body = '\n\n'.join(parts[1:])[:3000]
        n += write_pair(f"How to: {name}", body[:3000], 'scraped:skill', tags=['skill', name])
    counts['skills'] = n

# Save state
STATE.write_text(json.dumps({'seen': list(seen)[:50000]}))  # cap state size

total = sum(counts.values())
print(f"[scraped-to-surrogate] total_new={total} " + ' '.join(f"{k}={v}" for k,v in counts.items() if v > 0))
PYEOF

# Summary
lines=$(wc -l < "$OUT_FILE" 2>/dev/null || echo 0)
echo "[$(date '+%H:%M:%S')] $OUT_FILE = $lines total lines" >> "$LOG"
