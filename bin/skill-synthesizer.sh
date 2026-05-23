#!/usr/bin/env bash
# Scan recent decisions + sessions for REPEATED patterns — synthesize into skills
# Runs every hour. Claude reviews candidates, creates skill if pattern is reusable.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
LOG="$HOME/.claude/logs/skill-synthesizer.log"
SKILLS_DIR="$HOME/.hermes/skills/auto-synthesized"
mkdir -p "$SKILLS_DIR" "$(dirname "$LOG")"

# Collect last 30 decisions + retro lessons
INPUT=$(mktemp)
{
    echo "=== RECENT DECISIONS (last 30) ==="
    find "$SHARED/decisions" -name "*.md" -mtime -1 2>/dev/null | sort | tail -30 | while read f; do
        echo "--- $(basename $f) ---"
        head -40 "$f"
    done
    echo ""
    echo "=== RECENT LESSONS ==="
    tail -200 ~/.claude/memory/lessons_learned.md 2>/dev/null
    echo ""
    echo "=== EXISTING SKILLS (don't duplicate) ==="
    find ~/.hermes/skills -name "SKILL.md" 2>/dev/null | while read f; do
        grep -E "^name:" "$f" 2>/dev/null
    done | sort | uniq
} > "$INPUT"

# Ask Claude to identify reusable patterns worth creating as skills
PROMPT="You analyze autonomous agent work for REUSABLE PATTERNS worth becoming skills.

Look at the recent decisions + lessons below. For each repeated pattern that would benefit from standardization, propose a skill.

OUTPUT strict JSON (one skill per line, max 3):
{\"skill_name\":\"<lowercase-kebab>\",\"description\":\"<1 sentence>\",\"tags\":[\"tag1\",\"tag2\"],\"rationale\":\"<why this pattern is worth capturing>\",\"steps\":[\"<step 1>\",\"<step 2>\",\"<step 3>\",\"<step 4>\",\"<step 5>\"]}

Strict filters:
- SKIP if pattern only appeared once (need ≥2 occurrences)
- SKIP if already exists in the 'existing skills' list  
- SKIP if pattern is axentx-internal (too specific)
- KEEP if domain-general (DevSecOps, testing, research, code patterns)

INPUT:
$(cat "$INPUT" | head -500)"

echo "[$(date +%H:%M)] analyzing for new skills" >> "$LOG"
RESPONSE=$(echo "$PROMPT" | /opt/surrogate-1-harvest/bin/claude-bridge.sh --model opus --force --timeout 120 2>>"$LOG")
[[ -z "$RESPONSE" ]] && { echo "  bridge failed" >> "$LOG"; rm "$INPUT"; exit 1; }

# Parse JSON lines — create each skill
python3 <<PYEOF 2>>"$LOG"
import json, re, os, datetime
text = """$RESPONSE"""
matches = re.findall(r'\{[^{}]*"skill_name"[^{}]*\}', text, re.DOTALL)
created = 0
for m in matches:
    try:
        s = json.loads(m)
        name = s.get('skill_name','').strip().lower().replace(' ','-')
        if not name: continue
        # Skip if exists
        if os.path.exists(f"$SKILLS_DIR/{name}/SKILL.md"): continue
        os.makedirs(f"$SKILLS_DIR/{name}", exist_ok=True)
        # Write to staging first — gate via security-scan + spec-validate before committing
        staging_path = f"$SKILLS_DIR/{name}/SKILL.md.staged"
        with open(staging_path, 'w') as f:
            f.write(f'''---
name: {name}
description: {s.get('description','')}
version: 1.0.0
author: HermesSynthesizer
tags: {json.dumps(s.get('tags',[]))}
created_at: {datetime.datetime.now().isoformat()}
---

# {name.replace('-',' ').title()}

## Rationale
{s.get('rationale','')}

## Steps
''')
            for i, step in enumerate(s.get('steps',[]), 1):
                f.write(f"{i}. {step}\n")

        # SECURITY GATE — port from FrancyJGLisboa/agent-skill-creator
        import subprocess
        sec = subprocess.run(['/opt/surrogate-1-harvest/bin/skill-security-scan.sh', staging_path],
                            capture_output=True, timeout=20)
        spec = subprocess.run(['/opt/surrogate-1-harvest/bin/skill-spec-validate.sh', staging_path],
                             capture_output=True, timeout=20)

        if sec.returncode != 0:
            print(f"  🚫 BLOCKED (security): {name} — {sec.stderr.decode()[:200]}")
            os.rename(staging_path, f"$SKILLS_DIR/{name}/SKILL.md.rejected-security")
            continue
        if spec.returncode != 0:
            print(f"  ⚠️ BLOCKED (spec): {name} — {spec.stderr.decode()[:200]}")
            os.rename(staging_path, f"$SKILLS_DIR/{name}/SKILL.md.rejected-spec")
            continue

        # PASSED both gates — promote staged → real SKILL.md
        os.rename(staging_path, f"$SKILLS_DIR/{name}/SKILL.md")
        created += 1
        print(f"  ✅ auto-skill (sec+spec PASSED): {name}")
    except Exception as e:
        print(f"  parse err: {e}")
print(f"\\nTotal skills created: {created}")
PYEOF

rm -f "$INPUT"
echo "[$(date +%H:%M)] done" >> "$LOG"
