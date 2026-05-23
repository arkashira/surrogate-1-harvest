#!/usr/bin/env bash
# Hermes Trace Recorder — captures every Hermes decision/action as training data
# for Surrogate-1. Records multi-step agent workflows, tool chains, and outcomes.
#
# Output format (Alpaca-compat): {instruction, input, output, category}
# Destination: ~/axentx/surrogate/data/training-jsonl/hermes-trace-YYYY-MM-DD.jsonl
#
# Sources captured:
#   1. Cron execution decisions (what ran + why)
#   2. Healer signal → fix traces (diagnosis pattern)
#   3. Worker pipeline: spec → code → validation → review → synth → commit
#   4. BD agent decisions: theme → research → spec → priority
#   5. Tournament winners + reasoning
#   6. Skill synthesis decisions
#   7. Budget routing decisions (which provider, why)
#
# Runs every 2h. Incremental — tracks last-ingested mtime per source.
set -u

LOG="$HOME/.claude/logs/hermes-trace-recorder.log"
OUT_DIR="$HOME/axentx/surrogate/data/training-jsonl"
STATE="$HOME/.claude/state/hermes-trace-state.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR" "$(dirname "$STATE")"
[[ ! -f "$STATE" ]] && echo '{}' > "$STATE"

OUT_FILE="$OUT_DIR/hermes-trace-$(date +%Y-%m-%d).jsonl"

export OUT_FILE STATE LOG
"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import json, os, re, hashlib
from pathlib import Path
from datetime import datetime, timezone

HOME = Path.home()
OUT = Path(os.environ['OUT_FILE'])
STATE_PATH = Path(os.environ['STATE'])

try: state = json.loads(STATE_PATH.read_text())
except: state = {}

seen = set(state.get('seen', []))
def h(t): return hashlib.sha1((t or '')[:200].encode()).hexdigest()[:16]

def write_pair(instruction, response, source, category):
    hh = h(instruction)
    if hh in seen: return 0
    seen.add(hh)
    pair = {
        'instruction': instruction[:3000],
        'input': '',
        'output': response[:5000],
        'category': category,
        'source': f'hermes-trace:{source}',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }
    with open(OUT, 'a') as f:
        f.write(json.dumps(pair, ensure_ascii=False) + "\n")
    return 1

counts = {}

# ── 1. Cron execution decisions (jobs.json → learned routing) ──
try:
    jobs = json.loads((HOME / '.hermes/cron/jobs.json').read_text()).get('jobs', [])
    n = 0
    for j in jobs:
        name = j.get('name','')
        if not name: continue
        expr = j.get('schedule',{}).get('expr','?')
        script = j.get('script','') or j.get('prompt','')
        purpose = (j.get('paused_reason') or '').strip()
        if not script: continue
        instr = f"When should the cron job '{name}' run and what does it do?"
        resp = (
            f"Schedule: {expr}\n"
            f"Implementation: {script[:300]}\n"
            f"Context: This is part of Hermes's {('disabled' if not j.get('enabled', True) else 'active')} cron pipeline. "
            f"{purpose}"
        )
        n += write_pair(instr, resp, 'cron', 'orchestration')
    counts['cron-rationale'] = n
except Exception as e:
    print(f"[trace] cron fail: {e}")

# ── 2. Healer signal → fix traces ──
healer_dir = HOME / '.hermes/workspace/healer'
if healer_dir.exists():
    n = 0
    for log in sorted(healer_dir.glob('*.apply.log'))[-30:]:
        try: content = log.read_text()
        except: continue
        # Each line: timestamp + signal + CMD + RC + OUT
        for block in re.findall(r'---\s*(\S+)\s*---\nCMD:\s*(.+?)\nRC:\s*(\d+)\nOUT:', content, re.DOTALL):
            signal, cmd, rc = block
            if len(cmd) > 400 or len(cmd) < 10: continue
            instr = f"Hermes healer detected signal '{signal}'. What fix command should run?"
            resp = f"Command: {cmd.strip()[:400]}\nExpected RC: {rc}\n(Healer pattern: if signal appears, apply this safe-fix automatically.)"
            n += write_pair(instr, resp, 'healer', 'self-healing')
    counts['healer-fixes'] = n

# ── 3. Tournament winners + reasoning ──
t_dir = HOME / '.hermes/workspace/tournaments'
if t_dir.exists():
    n = 0
    for tf in sorted(t_dir.glob('*_tournament.json'))[-50:]:
        try: td = json.loads(tf.read_text())
        except: continue
        prio = td.get('prio','')
        winner = td.get('winner','')
        candidates = td.get('candidates',[])
        verdicts = td.get('verdicts','')
        if not prio or not winner: continue
        instr = f"Priority {prio} had {len(candidates)} worker candidates. Which wins and why?"
        resp = f"Winner: {winner}\nReasoning: {str(verdicts)[:1000]}"
        n += write_pair(instr, resp, 'tournament', 'agent-judgment')
    counts['tournament-decisions'] = n

# ── 4. Review verdicts + bugs (teaches reviewer skill) ──
review_dir = HOME / '.hermes/workspace/qwen-coder-reviews'
if review_dir.exists():
    n = 0
    for rf in sorted(review_dir.glob('*.review.json'))[-50:]:
        try:
            txt = rf.read_text()
            m = re.search(r'\{.*\}', txt, re.DOTALL)
            d = json.loads(m.group(0)) if m else {}
        except: continue
        if not d.get('verdict'): continue
        basename = rf.stem.replace('.review','')
        instr = f"Review code output '{basename}'. What's the verdict?"
        resp = (
            f"Verdict: {d.get('verdict','?')}\n"
            f"Quality: {d.get('quality_score','?')}/10\n"
            f"Bugs found: {d.get('bugs', [])[:3]}\n"
            f"Hallucinations: {d.get('hallucinations', [])[:3]}\n"
            f"Reasoning: {(d.get('reason','') or d.get('rationale',''))[:500]}"
        )
        n += write_pair(instr, resp, 'review', 'code-review')
    counts['review-verdicts'] = n

# ── 5. BD agent research → spec traces ──
research_dir = HOME / '.hermes/workspace/swarm-shared/research'
if research_dir.exists():
    n = 0
    for rf in research_dir.glob('*.md'):
        try: content = rf.read_text()
        except: continue
        if len(content) < 500: continue
        # Extract first H1 + first H2 + first 1500 chars
        title_m = re.match(r'^#\s+(.+)', content)
        title = title_m.group(1) if title_m else rf.stem
        instr = f"Research: {title}. What's the top recommendation?"
        # Find "## 5. Recommended" or "## Top" or similar
        rec_m = re.search(r'##\s+[0-9]*\.?\s*(Top|Recommend)(.+?)(?=\n##|\Z)', content, re.DOTALL)
        resp = rec_m.group(0)[:2000] if rec_m else content[:2000]
        n += write_pair(instr, resp, 'bd-research', 'research-analysis')
    counts['bd-research'] = n

# ── 6. Skill synthesis decisions ──
skills_dir = HOME / '.hermes/skills/auto-synthesized'
if skills_dir.exists():
    n = 0
    for s_dir in skills_dir.iterdir():
        if not s_dir.is_dir(): continue
        skill_file = s_dir / 'SKILL.md'
        if not skill_file.exists(): continue
        try: content = skill_file.read_text()
        except: continue
        name = s_dir.name
        instr = f"What reusable skill has Hermes auto-synthesized from patterns called '{name}'?"
        resp = content[:2500]
        n += write_pair(instr, resp, 'skill-synth', 'skill-creation')
    counts['skill-synth'] = n

# ── 7. Priority-to-commit traces (full pipeline examples) ──
try:
    pri = json.loads((HOME / '.hermes/workspace/swarm-shared/priority.json').read_text())
    specs_dir = HOME / '.hermes/workspace/swarm-shared/specs'
    n = 0
    for p in pri.get('priorities', [])[:30]:
        pid = p.get('id','')
        spec = specs_dir / f"{pid}.md"
        if not spec.exists(): continue
        try: spec_content = spec.read_text()[:3000]
        except: continue
        instr = f"Given priority {pid} ({p.get('title','')[:100]}), what's the implementation spec?"
        resp = spec_content
        n += write_pair(instr, resp, 'priority-spec', 'spec-implementation')
    counts['priority-specs'] = n
except Exception as e:
    print(f"[trace] priority fail: {e}")

# Save state
state['seen'] = list(seen)[:100000]
STATE_PATH.write_text(json.dumps(state))

total = sum(counts.values())
print(f"[trace] total_new={total} " + ' '.join(f"{k}={v}" for k,v in counts.items() if v > 0))
PYEOF

echo "[$(date '+%H:%M:%S')] trace-recorder done" >> "$LOG"
