#!/usr/bin/env bash
# Tournament Synthesis — instead of just picking 1 winner,
# Sonnet SYNTHESIZES the best parts from all N candidates into a superior combined output.
# Runs after tournament-review. If synthesis is materially better → use it instead of any single winner.
set -u

LOG="$HOME/.claude/logs/tournament-synthesis.log"
TOURNAMENT_DIR="$HOME/.hermes/workspace/tournaments"
OUT_DIR="$HOME/.hermes/workspace/dev-cloud-synthesis"
mkdir -p "$(dirname "$LOG")" "$OUT_DIR"

echo "[$(date '+%H:%M:%S')] synthesis scan" >> "$LOG"

# Find tournaments from last 2h that haven't been synthesized
/usr/bin/python3 <<'PYEOF' >> "$LOG"
import json, os, subprocess, time, re
from pathlib import Path

TOURNAMENT_DIR = Path.home() / ".hermes/workspace/tournaments"
OUT_DIR = Path.home() / ".hermes/workspace/dev-cloud-synthesis"
SYNTH_STATE = Path.home() / ".claude/state/synthesized.txt"
SYNTH_STATE.parent.mkdir(parents=True, exist_ok=True)
SYNTH_STATE.touch()

synthesized = set(SYNTH_STATE.read_text().splitlines())
cutoff = time.time() - 7200

count = 0
for tf in TOURNAMENT_DIR.glob("*_tournament.json"):
    if tf.stat().st_mtime < cutoff: continue
    if tf.name in synthesized: continue

    try: verdict = json.load(open(tf))
    except: continue

    candidates = verdict.get('candidates', [])
    if len(candidates) < 2: continue

    prio = verdict.get('prio', '')
    # Read all N candidates
    sections = []
    for p in candidates[:6]:
        pth = Path(p)
        if not pth.exists(): continue
        worker = pth.parent.name.replace('dev-cloud-','').replace('qwen-coder','qwen-local')
        body = pth.read_text()[:3500]
        sections.append(f"=== {worker} ===\n{body}")

    if len(sections) < 2: continue

    synth_prompt = f"""You are the Synthesis Engineer. Given {len(sections)} implementations of priority '{prio}', create ONE superior combined version that takes the best parts from each.

Your job:
1. Identify unique strengths of each candidate (e.g., 'gemini has better error handling', 'samba has cleaner structure', 'qwen-local has more thorough tests')
2. Combine those strengths into a SINGLE implementation that's better than any individual
3. If all candidates are mostly identical, pick the cleanest + add one improvement

Output STRICT structure (like original worker output):

## Implementation Plan
...

## Code
```<language>
# synthesized from: worker1 + worker2 + worker3
```

## Tests
```<language>
# combined best tests
```

## Acceptance Criteria
- 3 bullets

## Synthesis Notes
- Took X from <worker>, Y from <worker>, improved Z

{chr(10).join(sections)}
"""
    try:
        result = subprocess.run(
            ["/opt/surrogate-1-harvest/bin/claude-bridge.sh", "--model", "sonnet", "--timeout", "240"],
            input=synth_prompt, capture_output=True, text=True, timeout=280
        )
        if result.returncode == 0 and result.stdout:
            # Write synthesis output same schema as workers
            from datetime import datetime
            date = datetime.now().strftime('%Y-%m-%d_%H-%M')
            out_path = OUT_DIR / f"{prio}_{date}.md"
            # Extract frontmatter fields from first candidate for consistency
            first_body = Path(candidates[0]).read_text() if Path(candidates[0]).exists() else ''
            proj_m = re.search(r'^project:\s*(\S+)', first_body, re.MULTILINE)
            title_m = re.search(r'^title:\s*(.+)', first_body, re.MULTILINE)
            project = proj_m.group(1) if proj_m else '?'
            title = title_m.group(1).strip() if title_m else '?'

            with open(out_path, 'w') as f:
                f.write(f"""---
priority_id: {prio}
project: {project}
title: {title}
model: tournament-synthesis (Sonnet merging {len(sections)} candidates)
worker: dev-cloud-synthesis
ran_at: {datetime.utcnow().isoformat()}Z
reviewed: false
synthesis_of: {len(sections)}
---

{result.stdout}
""")
            with open(SYNTH_STATE, 'a') as f: f.write(tf.name + '\n')
            count += 1
            print(f"✅ {prio}: synthesized {len(sections)} → {out_path.name}")
        else:
            print(f"⚠️ {prio}: synth failed rc={result.returncode}")
    except subprocess.TimeoutExpired:
        print(f"⚠️ {prio}: synth timeout")
    except Exception as e:
        print(f"⚠️ {prio}: synth error {e}")

print(f"total synthesized: {count}")
PYEOF
