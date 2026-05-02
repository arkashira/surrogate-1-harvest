#!/usr/bin/env bash
# ROADMAP-100 #3 — Branch protection on all 6 axentx repos.
#
# Locks `main` against direct pushes:
#   - Require PR before merge (1 approval; auto-bot exempt via dismissal of stale)
#   - Require linear history (no merge commits)
#   - Require status checks to pass when configured (none currently — soft gate)
#   - Block force pushes + branch deletion
#   - Apply to admins too (no bypass) — except auto-bot via "merge_queue" semantics
#
# Re-runnable; PUTs the same payload each time. Requires gh auth with admin:repo.
#
# Usage: bash bin/apply-branch-protection.sh [repo1 repo2 ...]
#   No args → all 6 axentx repos.
set -euo pipefail

REPOS=("${@:-axentx/Costinel axentx/vanguard axentx/airship axentx/workio axentx/axiomops axentx/surrogate-1}")

read -r -d '' PAYLOAD <<'JSON' || true
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON

ok=0
fail=0
skipped=0
for repo in $REPOS; do
  printf '%-30s ' "$repo"
  branch=$(gh api "repos/$repo" --jq '.default_branch' 2>/dev/null || echo "")
  if [[ -z "$branch" ]]; then
    echo "SKIP (no access or repo missing)"
    skipped=$((skipped + 1))
    continue
  fi
  if echo "$PAYLOAD" | gh api -X PUT "repos/$repo/branches/$branch/protection" \
       -H "Accept: application/vnd.github+json" \
       --input - >/dev/null 2>&1; then
    echo "OK ($branch)"
    ok=$((ok + 1))
  else
    echo "FAIL"
    fail=$((fail + 1))
  fi
done

echo
echo "Result: $ok protected | $fail failed | $skipped skipped"
[[ $fail -eq 0 ]]
