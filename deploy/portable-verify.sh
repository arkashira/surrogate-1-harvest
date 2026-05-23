#!/usr/bin/env bash
# Portable verification — health check across the pipeline.
#
# Run after bootstrap or update. Prints a one-screen status board:
#   - Daemon active count
#   - Pipeline queue depths (Supabase RPC)
#   - Cost-guard last finding
#   - Disk + memory pressure
#   - Last spawned product (commit-timestamp)
#
# Exit 0 if healthy, 1 if any red flag.
set -uo pipefail

readonly INSTALL_DIR="${AXENTX_HOME:-/opt/surrogate-1-harvest}"
readonly ENV_FILE="${AXENTX_ENV_FILE:-/etc/surrogate-coordinator.env}"

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

red=0

print_section() { printf '\n── %s ──\n' "$*"; }

# ── Daemons ─────────────────────────────────────────────────────────────────
print_section "DAEMONS"
total=$(ls /etc/systemd/system/axentx-*.service 2>/dev/null | wc -l | tr -d ' ')
active=$(systemctl list-units --type=service --state=active 'axentx-*' --no-legend 2>/dev/null | wc -l | tr -d ' ')
failed=$(systemctl list-units --type=service --state=failed 'axentx-*' --no-legend 2>/dev/null | awk '{print $1}')
printf '  active: %s/%s\n' "$active" "$total"
if [[ -n "$failed" ]]; then
    printf '  FAILED:\n'
    echo "$failed" | sed 's/^/    /'
    red=1
fi

# ── Pipeline queues ─────────────────────────────────────────────────────────
print_section "PIPELINE QUEUES"
if [[ -n "${SUPABASE_URL:-}" ]] && [[ -n "${SUPABASE_ANON_KEY:-}" ]]; then
    for stage in research validator market-research bd spawn business-synthesis \
                 design architect ux prd dev review qa commit mvp-validator; do
        n=$(curl -fsS -G \
            "$SUPABASE_URL/rest/v1/pipeline_items" \
            -H "apikey: $SUPABASE_ANON_KEY" \
            -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
            -H "Prefer: count=exact" \
            -H "Range: 0-0" \
            --data-urlencode "stage=eq.$stage" \
            --data-urlencode "select=id" \
            -D - 2>/dev/null | grep -i 'content-range:' | awk -F'/' '{print $NF}' | tr -d '\r' || echo "?")
        printf '  %-20s %s\n' "$stage" "$n"
    done
else
    printf '  (SUPABASE env not set — skipping queue check)\n'
fi

# ── Cost guard ──────────────────────────────────────────────────────────────
print_section "COST GUARD (last finding)"
state_file="$INSTALL_DIR/state/cost-guard.state.json"
if [[ -f "$state_file" ]]; then
    findings=$(jq -r '.last_findings // [] | join(" | ")' "$state_file" 2>/dev/null)
    last_check=$(jq -r '.last_check // 0' "$state_file" 2>/dev/null)
    age=$(( $(date +%s) - last_check ))
    printf '  age: %ss ago\n' "$age"
    printf '  %s\n' "$findings"
    if [[ $age -gt 1800 ]]; then
        printf '  ⚠ stale (>30min) — cost-guard may be down\n'
        red=1
    fi
else
    printf '  (no cost-guard state file yet)\n'
fi

# ── Disk + memory ────────────────────────────────────────────────────────────
print_section "RESOURCES"
disk_pct=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
disk_avail=$(df -h / | awk 'NR==2 {print $4}')
mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2*100}')
load=$(awk '{print $1, $2, $3}' /proc/loadavg)
printf '  disk: %s%% used (%s avail)\n' "$disk_pct" "$disk_avail"
printf '  mem:  %s%% used\n' "$mem_pct"
printf '  load: %s\n' "$load"
[[ $disk_pct -gt 85 ]] && { printf '  ⚠ disk pressure\n'; red=1; }
[[ $mem_pct -gt 90 ]] && { printf '  ⚠ memory pressure\n'; red=1; }

# ── Last spawned product ────────────────────────────────────────────────────
print_section "PRODUCTS"
projects_root="${AXENTX_ROOT:-/opt/axentx}"
if [[ -d "$projects_root" ]]; then
    count=$(find "$projects_root" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    printf '  total: %s spawned\n' "$count"
    # Latest 3 by mtime, with business pack status
    while IFS= read -r p; do
        slug=$(basename "$p")
        bdocs=$(ls "$p/business" 2>/dev/null | wc -l | tr -d ' ')
        committed=$(git -C "$p" log --oneline -1 2>/dev/null | head -c 80 || echo "no commits")
        printf '  %-25s business=%s/8  last=%s\n' "$slug" "$bdocs" "$committed"
    done < <(find "$projects_root" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
             | sort -rn | head -3 | cut -d' ' -f2-)
else
    printf '  ($projects_root does not exist yet)\n'
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
print_section "VERDICT"
if [[ $red -eq 0 ]]; then
    printf '  ✓ ALL GREEN\n'
    exit 0
else
    printf '  ✗ RED FLAGS — see above\n'
    exit 1
fi
