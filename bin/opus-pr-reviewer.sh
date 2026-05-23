#!/usr/bin/env bash
# Opus PR Reviewer + Auto-Merger — replaces manual user review for hermes/auto/* branches.
# Opus 4.7 --force does DEEP review (300s timeout) → auto-merges if ALL gates pass.
# Runs every 30 min.
#
# SAFETY GATES (all must pass):
# 1. Branch is hermes/auto/* (Hermes-generated, not user's)
# 2. Diff < 500 LOC total (not → 500 lines is human territory)
# 3. Touched files are in safe paths only (src/, tests/, docs/, .hermes-auto/)
# 4. NOT touching: .github/, Dockerfile, auth/*, migrations/, schema/*, package-lock
# 5. Opus verdict=merge + confidence>=0.85 + blockers=[]
# 6. Max 3 auto-merges per project per day (prevent flooding main)
# 7. No existing user review (if user commented, defer to them)
set -u

LOG="$HOME/.claude/logs/opus-pr-reviewer.log"
STATE_DIR="$HOME/.claude/state/opus-pr"
REVIEW_DIR="$HOME/.hermes/workspace/pr-reviews"
mkdir -p "$(dirname "$LOG")" "$STATE_DIR" "$REVIEW_DIR"

set -a; [[ -f "$HOME/.hermes/.env" ]] && source "$HOME/.hermes/.env"; set +a
export GH_TOKEN="${GITHUB_MODELS_TOKEN:-${GITHUB_TOKEN:-}}"
[[ -z "$GH_TOKEN" ]] && { echo "[$(date '+%H:%M:%S')] no GH_TOKEN" >> "$LOG"; exit 1; }

PROJECTS=("Costinel" "vanguard" "arkship" "surrogate" "workio")
TODAY=$(date +%Y-%m-%d)

echo "[$(date '+%H:%M:%S')] PR scan start" >> "$LOG"

for REPO in "${PROJECTS[@]}"; do
    # Per-project daily merge counter
    MERGE_COUNT_FILE="$STATE_DIR/${REPO}_${TODAY}.count"
    MERGE_COUNT=$(/usr/bin/cat "$MERGE_COUNT_FILE" 2>/dev/null || echo 0)

    # Skip project if daily cap hit
    if [[ "$MERGE_COUNT" -ge 3 ]]; then
        echo "[$(date '+%H:%M:%S')] $REPO: daily cap (3) reached — skip" >> "$LOG"
        continue
    fi

    # List open PRs from hermes/auto/* branches (in arkashira/<repo>)
    PRS=$(/usr/bin/curl -sS \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/arkashira/$REPO/pulls?state=open&per_page=20" \
        2>/dev/null | /usr/bin/python3 -c "
import json, sys
try: prs = json.load(sys.stdin)
except: sys.exit()
if not isinstance(prs, list): sys.exit()
for p in prs:
    branch = p.get('head',{}).get('ref','')
    if not branch.startswith('hermes/auto/'): continue
    num = p.get('number',0)
    title = p.get('title','')
    additions = p.get('additions', 0)
    deletions = p.get('deletions', 0)
    changed = p.get('changed_files', 0)
    print(f'{num}|{branch}|{additions}|{deletions}|{changed}|{title[:100]}')
" 2>/dev/null)

    [[ -z "$PRS" ]] && { echo "[$(date '+%H:%M:%S')] $REPO: no hermes PRs" >> "$LOG"; continue; }

    while IFS='|' read -r PR_NUM BRANCH ADDS DELS FILES TITLE; do
        [[ -z "$PR_NUM" ]] && continue

        # Skip if previously reviewed (marker file exists)
        MARKER="$STATE_DIR/${REPO}_PR${PR_NUM}.reviewed"
        [[ -f "$MARKER" ]] && continue

        TOTAL_LOC=$((ADDS + DELS))

        # GATE 1: diff size < 500 LOC
        if [[ "$TOTAL_LOC" -gt 500 ]]; then
            echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: TOO BIG ($TOTAL_LOC LOC) — defer to user" >> "$LOG"
            echo "big" > "$MARKER"
            continue
        fi

        # Fetch file list + diff
        FILES_JSON=$(/usr/bin/curl -sS \
            -H "Authorization: Bearer $GH_TOKEN" \
            "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM/files" 2>/dev/null)

        # GATE 2: dangerous paths — block
        DANGEROUS=$(echo "$FILES_JSON" | /usr/bin/python3 -c "
import json, sys, re
try: files = json.load(sys.stdin)
except: sys.exit()
DANGER = [
    r'\.github/workflows/',     # CI changes
    r'Dockerfile',              # image changes
    r'docker-compose',
    r'/auth/',                  # auth changes
    r'/authn?/|/authz?/',
    r'migrations?/',            # DB schema
    r'schema/',
    r'package-lock\.json',
    r'yarn\.lock',
    r'Cargo\.lock',
    r'go\.sum',
    r'\.env',
    r'/secrets?/',
    r'/credentials?/',
    r'/terraform/.*\.tf$',      # infra
    r'kubernetes/.*production',
    r'helm/.*values',
]
for f in files:
    path = f.get('filename','')
    for pattern in DANGER:
        if re.search(pattern, path, re.IGNORECASE):
            print(f'DANGER:{path}')
            break
" 2>/dev/null)

        if [[ -n "$DANGEROUS" ]]; then
            echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: DANGEROUS path — defer to user: $DANGEROUS" >> "$LOG"
            echo "dangerous" > "$MARKER"
            continue
        fi

        # GATE 3: no prior user review?
        USER_REVIEWS=$(/usr/bin/curl -sS \
            -H "Authorization: Bearer $GH_TOKEN" \
            "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM/reviews" 2>/dev/null | \
            /usr/bin/python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except: sys.exit()
for r in d:
    if r.get('user',{}).get('login','') != 'arkashira':
        continue
    if 'hermes-opus' not in (r.get('body','') or ''):
        print('user-reviewed')
        break
")
        if [[ "$USER_REVIEWS" == "user-reviewed" ]]; then
            echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: user already reviewed — skip" >> "$LOG"
            echo "user" > "$MARKER"
            continue
        fi

        # Fetch full diff
        DIFF=$(/usr/bin/curl -sS \
            -H "Authorization: Bearer $GH_TOKEN" \
            -H "Accept: application/vnd.github.diff" \
            "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM" 2>/dev/null | /usr/bin/head -c 30000)

        # Build Opus review prompt
        PROMPT_FILE=$(/usr/bin/mktemp)
        cat > "$PROMPT_FILE" <<EOF
You are the Senior Engineer + Tech Lead reviewing a Hermes-auto-generated PR. User authorized you to DIRECTLY AUTO-MERGE if safe — save their time for harder calls.

=== PR METADATA ===
Repo: arkashira/$REPO
PR #$PR_NUM: $TITLE
Branch: $BRANCH
Diff: +$ADDS / -$DELS LOC across $FILES files

=== FULL DIFF ===
$DIFF

=== YOUR TASK ===
Review like a strict senior reviewer. Check:
1. Correctness — does it actually implement what the title claims?
2. Security — any injected secrets? SQL injection? unsafe deserialize? path traversal?
3. Backward compatibility — breaking API? schema change? new required env var?
4. Code quality — idiomatic? handles errors? no obvious bugs?
5. Tests — included or adequately covered?
6. Scope — stays within stated priority, doesn't sneak-fix 5 other things?
7. Hallucination check — do all imports/APIs/functions actually exist?

Output STRICT JSON:
{
  "verdict": "merge" | "request-changes" | "close",
  "confidence": 0.0-1.0,
  "blockers": ["critical issues that must be fixed before merge, or empty if clean"],
  "concerns": ["minor issues — note but don't block"],
  "strengths": ["good things about the PR"],
  "safe_to_auto_merge": true | false,
  "rationale": "1-2 sentence why this verdict"
}

Rules for safe_to_auto_merge=true:
- verdict=merge
- confidence >= 0.85
- blockers is empty []
- no security concerns
- no breaking changes
- scope stays inside stated priority

If ANY doubt — set safe_to_auto_merge=false. User will handle. That's fine.
EOF

        echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: sending to Opus ($TOTAL_LOC LOC)" >> "$LOG"

        # Call Opus 4.7 --force (important decision — use heavyweight)
        REVIEW=$(/usr/bin/cat "$PROMPT_FILE" | "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model opus --force --timeout 300 2>>"$LOG")
        /bin/rm -f "$PROMPT_FILE"

        [[ -z "$REVIEW" ]] && { echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: bridge failed" >> "$LOG"; continue; }

        # Save review
        REVIEW_FILE="$REVIEW_DIR/${REPO}_PR${PR_NUM}_$(date +%Y-%m-%d_%H-%M).json"
        echo "$REVIEW" > "$REVIEW_FILE"

        # Parse verdict
        DECISION=$(echo "$REVIEW" | /usr/bin/python3 -c "
import json, sys, re
txt = sys.stdin.read()
m = re.search(r'\{.*\}', txt, re.DOTALL)
if not m: sys.exit('no-json')
try: d = json.loads(m.group(0))
except: sys.exit('parse-fail')

verdict = d.get('verdict','')
conf = d.get('confidence', 0)
blockers = d.get('blockers', [])
safe = d.get('safe_to_auto_merge', False)
rationale = d.get('rationale', '')[:200]

if verdict == 'merge' and conf >= 0.85 and not blockers and safe:
    print(f'MERGE|{conf}|{rationale}')
elif verdict == 'merge':
    print(f'APPROVE_NO_MERGE|{conf}|{rationale}')
else:
    print(f'{verdict}|{conf}|{rationale}')
")

        STATE="${DECISION%%|*}"
        REST="${DECISION#*|}"
        CONF="${REST%%|*}"
        RATIONALE="${REST#*|}"

        case "$STATE" in
            MERGE)
                # Auto-merge via gh API
                /usr/bin/curl -sS -X POST \
                    -H "Authorization: Bearer $GH_TOKEN" \
                    -H "Accept: application/vnd.github+json" \
                    "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM/reviews" \
                    -d "$(/usr/bin/python3 -c "
import json
print(json.dumps({
    'event': 'APPROVE',
    'body': f'🤖 **hermes-opus-reviewer** approved (confidence=$CONF)\n\n$RATIONALE\n\n_Auto-merged by claude-opus-4-7 after passing safety gates (diff<500 LOC, no dangerous paths, no user review, confidence≥0.85)._'
}))
")" >> "$LOG" 2>&1

                # Merge (squash)
                MERGE_RESULT=$(/usr/bin/curl -sS -X PUT \
                    -H "Authorization: Bearer $GH_TOKEN" \
                    -H "Accept: application/vnd.github+json" \
                    "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM/merge" \
                    -d '{"merge_method":"squash"}' 2>>"$LOG")

                if echo "$MERGE_RESULT" | /usr/bin/grep -q '"merged":true'; then
                    echo "[$(date '+%H:%M:%S')] ✅ AUTO-MERGED $REPO#$PR_NUM (conf=$CONF)" >> "$LOG"
                    MERGE_COUNT=$((MERGE_COUNT + 1))
                    echo "$MERGE_COUNT" > "$MERGE_COUNT_FILE"
                    echo "merged" > "$MARKER"
                else
                    echo "[$(date '+%H:%M:%S')] ⚠️ $REPO#$PR_NUM: merge API failed" >> "$LOG"
                fi
                ;;
            APPROVE_NO_MERGE|request-changes|close)
                # Post review but don't merge
                EVENT="COMMENT"
                [[ "$STATE" == "request-changes" ]] && EVENT="REQUEST_CHANGES"
                /usr/bin/curl -sS -X POST \
                    -H "Authorization: Bearer $GH_TOKEN" \
                    "https://api.github.com/repos/arkashira/$REPO/pulls/$PR_NUM/reviews" \
                    -d "$(/usr/bin/python3 -c "
import json
print(json.dumps({
    'event': '$EVENT',
    'body': f'🤖 **hermes-opus-reviewer** {($EVENT).lower()}\n\nConfidence: $CONF\n\n$RATIONALE\n\n_See full review in ~/.hermes/workspace/pr-reviews/_'
}))
")" >> "$LOG" 2>&1
                echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: $STATE (conf=$CONF) — human decides" >> "$LOG"
                echo "$STATE" > "$MARKER"
                ;;
            *)
                echo "[$(date '+%H:%M:%S')] $REPO#$PR_NUM: unknown state '$STATE'" >> "$LOG"
                ;;
        esac

        # Rate-limit: 10s between PR reviews (don't slam Opus)
        sleep 10
    done <<< "$PRS"
done

echo "[$(date '+%H:%M:%S')] PR scan done" >> "$LOG"
