#!/usr/bin/env bash
# Deep code scraper — pulls .py/.ts/.go/.rs/.java source files (not just docs).
# Captures code style, implementation patterns, real-world idioms.
# Usage: scrape-code-deep.sh <role>
set -u
ROLE="${1:?role required}"
CATALOG="$HOME/.hermes/config/domain-experts.json"
WORK="/tmp/code-deep-$ROLE"
SEEN="$HOME/.claude/state/code-deep-seen-$ROLE.txt"
LOG="$HOME/.claude/logs/scrape-code-deep-$ROLE.log"
MIN_FREE_GB=5
MAX_REPOS=12           # fewer repos but pull deeper
MAX_FILES_PER_REPO=300 # 10x more files than shallow
MAX_SIZE=150000        # 150KB — most source files fit

mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"
touch "$SEEN"
echo "[$(date '+%Y-%m-%d %H:%M')] code-deep:$ROLE" | tee -a "$LOG"

REPOS=$(python3 -c "
import json
d = json.load(open('$CATALOG'))
role = d['roles'].get('$ROLE', {})
for r in role.get('github_repos',[]): print(r)
")
[[ -z "$REPOS" ]] && exit 1

COUNT=0
for REPO in $REPOS; do
    [[ $COUNT -ge $MAX_REPOS ]] && break
    FREE_GB=$(df -g ~ | tail -1 | awk '{print $4}')
    [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]] && break
    
    grep -qxF "$REPO" "$SEEN" && continue
    
    DIR="$WORK/${REPO//\//_}"
    echo "  [$(date +%H:%M:%S)] $REPO" | tee -a "$LOG"
    git clone --depth 1 --filter=blob:limit=200k "https://github.com/$REPO.git" "$DIR" 2>>"$LOG" || { echo "$REPO" >> "$SEEN"; continue; }
    
    python3 <<PY 2>>"$LOG"
import os, glob, sqlite3, datetime
from pathlib import Path

DB = str(Path.home() / '.claude/index.db')
ROOT, REPO, ROLE = "$DIR", "$REPO", "$ROLE"
conn = sqlite3.connect(DB)
cur = conn.cursor()

# BROAD patterns — actual source code + configs + docs
PATTERNS = [
    # Docs
    'README*', '*.md', '*.mdx', 'docs/**/*.md', 'docs/**/*.mdx',
    'CONTRIBUTING*', 'ARCHITECTURE*',
    # Source code — ALL languages
    '*.py', 'src/**/*.py', 'lib/**/*.py', 'app/**/*.py',
    '*.ts', '*.tsx', 'src/**/*.ts', 'src/**/*.tsx',
    '*.js', '*.jsx', 'src/**/*.js', 'src/**/*.jsx',
    '*.go', 'cmd/**/*.go', 'pkg/**/*.go', 'internal/**/*.go',
    '*.rs', 'src/**/*.rs',
    '*.java', 'src/**/*.java',
    '*.rb', '*.kt', '*.swift', '*.php',
    # Configs — style + best practices
    '.eslintrc*', '.prettierrc*', '.ruff.toml', 'pyproject.toml',
    'tsconfig.json', 'tsconfig*.json', 'biome.json',
    '.github/workflows/*.yml', '.github/workflows/*.yaml',
    'Dockerfile*', 'docker-compose*.yml',
    'Makefile', 'justfile', 'Taskfile.yml',
    # Examples + tests (show usage patterns)
    'examples/**/*.py', 'examples/**/*.ts', 'examples/**/*.go',
    'tests/**/*.py', 'test/**/*.ts', 'tests/**/*.go',
    'e2e/**/*.ts', '__tests__/**/*.ts',
    # Infra patterns
    '**/*.tf', 'terraform/**/*', 'k8s/**/*.yaml', 'helm/**/*.yaml',
    '**/manifest.yaml', '**/values.yaml',
]

SKIP = ['node_modules', '.git/', 'dist/', 'build/', 'target/',
        '__pycache__', '.venv', 'vendor/', '.next/', 'coverage/']

added, seen_paths = 0, set()
for pat in PATTERNS:
    if added >= $MAX_FILES_PER_REPO: break
    for f in glob.glob(f"{ROOT}/{pat}", recursive=True):
        if added >= $MAX_FILES_PER_REPO: break
        if not os.path.isfile(f) or f in seen_paths: continue
        if any(s in f for s in SKIP): continue
        try:
            size = os.path.getsize(f)
            if size < 100 or size > $MAX_SIZE: continue
            content = open(f, encoding='utf-8', errors='ignore').read()[:$MAX_SIZE]
            rel = f.replace(ROOT+'/','')
            ext = rel.split('.')[-1] if '.' in rel else ''
            topic = f"{ext}-code" if ext in ('py','ts','tsx','go','rs','java','rb','js','jsx') else 'docs'
            seen_paths.add(f)
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        (f'code-deep:{ROLE}', REPO, f'github:{REPO}/{rel}', topic, rel, content,
                         datetime.datetime.now().isoformat()))
            added += 1
        except: pass
conn.commit()
print(f"  + {REPO}: {added} files (code+docs+config)")
PY
    rm -rf "$DIR"
    echo "$REPO" >> "$SEEN"
    COUNT=$((COUNT+1))
done

python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home() / '.claude/index.db'))
conn.execute(\"INSERT INTO docs_fts(docs_fts) VALUES('rebuild')\"); conn.commit()
" 2>>"$LOG"
echo "[$(date +%H:%M)] done: $COUNT repos" >> "$LOG"
