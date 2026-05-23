#!/usr/bin/env bash
# Backfill /business/ commits for products spawned BEFORE the
# axentx-business-synthesis-daemon git-commit fix (2026-05-03).
#
# Symptom: business pack written to disk at /opt/axentx/<slug>/business/
# but never `git add + commit + push`. GitHub repos showed only README.
#
# This script: for each spawned product, if /business/*.md exists locally
# but isn't tracked in git, stage + commit + push.
#
# Idempotent. Safe to run multiple times. Skips repos already in sync.
#
# Usage:
#   sudo -u <user-with-git-creds> bash deploy/backfill-stranded-products.sh
#   AXENTX_ROOT=/opt/axentx bash deploy/backfill-stranded-products.sh
set -uo pipefail

readonly PROJECTS_ROOT="${AXENTX_ROOT:-/opt/axentx}"
readonly DRY_RUN="${DRY_RUN:-0}"

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" >&2; }

[[ -d "$PROJECTS_ROOT" ]] || { log "no projects dir: $PROJECTS_ROOT"; exit 1; }

backfilled=0
skipped=0
failed=0

for repo_dir in "$PROJECTS_ROOT"/*/; do
    repo_dir=${repo_dir%/}
    slug=$(basename "$repo_dir")
    [[ -d "$repo_dir/.git" ]] || { log "  skip $slug (not a git repo)"; continue; }

    # Has /business/ been written?
    if ! ls "$repo_dir/business"/*.md &>/dev/null; then
        skipped=$((skipped + 1))
        continue
    fi

    # Anything to commit?
    if [[ -z "$(git -C "$repo_dir" status --porcelain business/ 2>/dev/null)" ]]; then
        # Maybe already committed but not pushed?
        ahead=$(git -C "$repo_dir" rev-list --count '@{u}..HEAD' 2>/dev/null || echo "0")
        if [[ "$ahead" -gt 0 ]]; then
            log "▸ $slug — committed but not pushed ($ahead commits ahead)"
            if [[ "$DRY_RUN" != "1" ]]; then
                git -C "$repo_dir" push origin HEAD:main 2>&1 | tail -3
                backfilled=$((backfilled + 1))
            fi
        else
            skipped=$((skipped + 1))
        fi
        continue
    fi

    log "▸ $slug — backfilling /business/ commit"
    [[ "$DRY_RUN" == "1" ]] && { log "  (dry-run)"; continue; }

    git -C "$repo_dir" add business/ 2>&1 | tail -2
    n=$(ls "$repo_dir/business"/*.md 2>/dev/null | wc -l | tr -d ' ')
    msg="business pack: $n sections (BMC, marketing, journey, dataflow, stories, tech, breakeven, partners) [backfill]"

    if GIT_AUTHOR_NAME="axentx-dev-bot" \
       GIT_AUTHOR_EMAIL="dev-bot@axentx.local" \
       GIT_COMMITTER_NAME="axentx-dev-bot" \
       GIT_COMMITTER_EMAIL="dev-bot@axentx.local" \
       git -C "$repo_dir" commit -m "$msg" 2>&1 | tail -3; then

        # Push, with rebase retry on non-fast-forward
        for attempt in 1 2; do
            if git -C "$repo_dir" push origin HEAD:main 2>&1 | tail -3; then
                log "  ✓ $slug pushed (attempt $attempt)"
                backfilled=$((backfilled + 1))
                break
            fi
            git -C "$repo_dir" pull --rebase origin main 2>&1 | tail -2
            [[ $attempt -eq 2 ]] && { log "  ✗ $slug push failed after rebase"; failed=$((failed + 1)); }
        done
    else
        log "  ✗ $slug commit failed"
        failed=$((failed + 1))
    fi
done

log "─────────────────────────────────────────"
log "BACKFILL SUMMARY"
log "  backfilled: $backfilled"
log "  skipped:    $skipped (clean / no business pack)"
log "  failed:     $failed"
[[ $failed -gt 0 ]] && exit 1
exit 0
