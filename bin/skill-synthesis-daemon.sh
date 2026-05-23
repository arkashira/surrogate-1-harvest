#!/usr/bin/env bash
# Skill-synthesis daemon — reads cloned/scraped repos in /tmp and ~/.hermes/workspace/,
# extracts reusable patterns (functions, prompts, tool definitions, configs), and
# writes them as Surrogate skills under ~/.surrogate/skills/<category>/<slug>/SKILL.md.
#
# Inspired by Voyager paper (skill library) + community skills (anthropic-skills).
# Each pattern → SKILL.md frontmatter + content + example invocation.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

SKILLS_DIR="$HOME/.surrogate/skills"
LOG="$HOME/.surrogate/logs/skill-synthesis.log"
PAIRS="$HOME/.surrogate/training-pairs.jsonl"
mkdir -p "$SKILLS_DIR" "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] skill-synthesis start" | tee -a "$LOG"

# ── Source dirs to scan for patterns ────────────────────────────────────────
SCAN_DIRS=(
    "/tmp/agentic-discovery"
    "$HOME/.hermes/workspace/surrogate-scrape"
    "$HOME/.hermes/workspace/projects"
)

while true; do
    for src in "${SCAN_DIRS[@]}"; do
        [[ ! -d "$src" ]] && continue

        # Find candidate files (small, recent, code/prompt-like)
        find "$src" -type f \( \
            -name "*.md" -o -name "*.py" -o -name "*.ts" -o -name "*.go" -o \
            -name "*.sh" -o -name "*.yaml" -o -name "*.toml" -o -name "*.json" \
        \) -size -50k -mtime -3 2>/dev/null | head -200 | while read -r f; do
            # Skip already-synthesized
            HASH=$(python3 -c "import hashlib; print(hashlib.md5(open('$f','rb').read()).hexdigest()[:12])" 2>/dev/null)
            [[ -z "$HASH" ]] && continue
            STAMP="$SKILLS_DIR/.synthesized/$HASH"
            [[ -f "$STAMP" ]] && continue
            mkdir -p "$(dirname "$STAMP")"

            python3 - "$f" "$SKILLS_DIR" "$PAIRS" "$STAMP" <<'PYEOF' 2>>"$LOG"
import sys, re, json, time, os, hashlib
from pathlib import Path

src_path, skills_dir, pairs_log, stamp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
src = Path(src_path)
content = src.read_text(errors="ignore")[:30000]

# Detect skill candidates by signal:
patterns = []

# 1. Python functions with descriptive docstrings (≥ 3 lines)
for m in re.finditer(r'def (\w+)\([^)]*\)[^:]*:\s*\n\s*"""([^"]{40,500})"""', content):
    name, doc = m.group(1), m.group(2).strip()
    if any(noisy in name.lower() for noisy in ("test_","_test","setup","teardown","__")): continue
    patterns.append(("python-fn", name, doc, m.group(0)[:2000]))

# 2. Tool/function-call schemas (JSON with name+description+parameters)
for m in re.finditer(r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"description"\s*:\s*"([^"]+)"\s*,\s*"parameters"', content):
    patterns.append(("tool-schema", m.group(1), m.group(2), m.group(0)[:1500]))

# 3. Prompt templates (markdown with role headers)
if re.search(r'#+\s*(System|Role|You are|Instructions)', content, re.IGNORECASE):
    title_m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    title = title_m.group(1) if title_m else src.stem
    patterns.append(("prompt-template", title[:80], content[:200].replace('\n',' '), content[:3000]))

# 4. Bash function declarations with comment header
for m in re.finditer(r'#\s*(.{20,200})\n([a-z_]+)\(\)\s*\{', content):
    desc, name = m.group(1).strip(), m.group(2)
    if name in ("main","init","cleanup"): continue
    patterns.append(("bash-fn", name, desc, m.group(0)[:1500]))

# Pick top 1 per file (avoid noise)
if not patterns:
    Path(stamp).touch()
    sys.exit(0)
ptype, name, summary, snippet = patterns[0]

# Slugify + categorize
slug = re.sub(r'[^a-z0-9-]+','-', name.lower()).strip('-')[:50]
category_map = {
    "python-fn":"code-python",
    "tool-schema":"agent-tools",
    "prompt-template":"prompts",
    "bash-fn":"ops-shell",
}
cat = category_map.get(ptype, "misc")
skill_dir = Path(skills_dir) / cat / slug
skill_dir.mkdir(parents=True, exist_ok=True)
skill_file = skill_dir / "SKILL.md"

# Don't overwrite existing skills with same slug — append number
if skill_file.exists():
    n = 2
    while (skill_dir.parent / f"{slug}-{n}").exists(): n += 1
    skill_dir = skill_dir.parent / f"{slug}-{n}"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"

frontmatter = f"""---
name: {name}
type: {ptype}
category: {cat}
source: {src.name}
synthesized_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}
---

# {name}

**Source:** `{src}`

## What it does
{summary[:300]}

## Pattern
```
{snippet}
```

## Invocation
[How Surrogate would use this skill — auto-generate via LLM next pass]
"""
skill_file.write_text(frontmatter)

# Push as training pair
pair = {
    "ts": time.time(),
    "source": "skill-synthesis",
    "skill_path": str(skill_file),
    "category": cat,
    "prompt": f"You have learned a new skill of type '{ptype}' named '{name}'. Use it when relevant.\n\nPattern:\n{snippet[:2000]}",
    "response": summary,
}
with open(pairs_log, "a") as f:
    f.write(json.dumps(pair, ensure_ascii=False) + "\n")

Path(stamp).touch()
print(f"  ✨ skill: {cat}/{skill_dir.name} from {src.name}")
PYEOF
        done
    done

    # Stats
    SKILL_COUNT=$(find "$SKILLS_DIR" -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')
    echo "[$(date +%H:%M:%S)] cycle done · total skills=$SKILL_COUNT" >> "$LOG"
    sleep 180   # 3 min between cycles
done
