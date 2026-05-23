#!/usr/bin/env bash
# Portable delta-update for axentx surrogate-1-harvest pipeline.
#
# Use this AFTER initial bootstrap to pull new code + restart only the
# daemons that actually changed. Idempotent. Safe to cron.
#
# Usage:
#   sudo bash /opt/surrogate-1-harvest/deploy/portable-update.sh
#   sudo AXENTX_HOME=/opt/surrogate-1-harvest bash deploy/portable-update.sh
#
# Behavior:
#   1. git fetch + rebase from origin/$BRANCH
#   2. pip install -r requirements.txt (no-op if unchanged)
#   3. Re-render any changed systemd units
#   4. Restart only daemons whose .py / .service changed
#   5. Health-check after restart
set -euo pipefail

readonly INSTALL_DIR="${AXENTX_HOME:-/opt/surrogate-1-harvest}"
readonly BRANCH="${AXENTX_BRANCH:-main}"
readonly ENV_FILE="${AXENTX_ENV_FILE:-/etc/surrogate-coordinator.env}"
readonly RUN_USER="${AXENTX_USER:-$(stat -c %U "$INSTALL_DIR" 2>/dev/null || echo ubuntu)}"

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" >&2; }
fail() { log "FATAL: $*"; exit "${2:-1}"; }

[[ $EUID -eq 0 ]] || fail "must run as root"
[[ -d "$INSTALL_DIR/.git" ]] || fail "not a repo: $INSTALL_DIR (run portable-bootstrap.sh first)"

# ── 1. Fetch + rebase ───────────────────────────────────────────────────────
log "fetching origin/$BRANCH"
old_sha=$(git -C "$INSTALL_DIR" rev-parse HEAD)
sudo -u "$RUN_USER" git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
sudo -u "$RUN_USER" git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
new_sha=$(git -C "$INSTALL_DIR" rev-parse HEAD)

if [[ "$old_sha" == "$new_sha" ]]; then
    log "already up-to-date at $old_sha — nothing to do"
    exit 0
fi
log "updated $old_sha → $new_sha"

# ── 2. Compute changed file list ────────────────────────────────────────────
changed=$(git -C "$INSTALL_DIR" diff --name-only "$old_sha" "$new_sha")
log "changed files:"
echo "$changed" | sed 's/^/  /' | head -30 >&2

# ── 3. requirements.txt: pip install only if changed ────────────────────────
if echo "$changed" | grep -q '^requirements.txt$'; then
    log "requirements.txt changed — pip install"
    sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
fi

# ── 4. Map changed bin/ files → systemd units ───────────────────────────────
to_restart=()
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    case "$f" in
        bin/axentx-*-daemon.py | bin/axentx_pipeline.py)
            # axentx_pipeline.py is shared — restart all axentx daemons
            if [[ "$f" == "bin/axentx_pipeline.py" ]]; then
                while read -r unit; do to_restart+=("$unit"); done < \
                    <(systemctl list-unit-files 'axentx-*.service' --no-legend | awk '{print $1}' | sed 's/.service$//')
                break
            fi
            base=$(basename "$f" .py)
            to_restart+=("$base")
            ;;
        bin/axentx-*-daemon.sh)
            base=$(basename "$f" .sh)
            to_restart+=("$base")
            ;;
        systemd/axentx-*.service)
            unit=$(basename "$f" .service)
            to_restart+=("$unit")
            # Re-install the unit (substitute paths again)
            sed -e "s|^User=.*|User=$RUN_USER|" \
                -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
                -e "s|^EnvironmentFile=.*|EnvironmentFile=$ENV_FILE|" \
                -e "s|REPO_ROOT=/opt/surrogate-1-harvest|REPO_ROOT=$INSTALL_DIR|g" \
                -e "s|/opt/surrogate-1-harvest/.venv/bin/python|$INSTALL_DIR/.venv/bin/python|g" \
                -e "s|/opt/surrogate-1-harvest/bin|$INSTALL_DIR/bin|g" \
                "$INSTALL_DIR/$f" > "/etc/systemd/system/${unit}.service"
            ;;
    esac
done <<< "$changed"

# Dedup
mapfile -t to_restart < <(printf '%s\n' "${to_restart[@]}" | sort -u)

if [[ ${#to_restart[@]} -eq 0 ]]; then
    log "no daemons need restart (only docs/non-code changed)"
    exit 0
fi

systemctl daemon-reload

# ── 5. Restart in batches of 4 to avoid memory burst ────────────────────────
log "restarting ${#to_restart[@]} daemon(s)"
i=0
for d in "${to_restart[@]}"; do
    if [[ -f "/etc/systemd/system/${d}.service" ]]; then
        systemctl restart "$d" || log "  ⚠ restart $d failed"
        i=$((i + 1))
        [[ $((i % 4)) -eq 0 ]] && sleep 2
    fi
done

# ── 6. Health check ─────────────────────────────────────────────────────────
sleep 5
inactive=()
for d in "${to_restart[@]}"; do
    [[ -f "/etc/systemd/system/${d}.service" ]] || continue
    if ! systemctl is-active --quiet "$d"; then
        inactive+=("$d")
    fi
done

if [[ ${#inactive[@]} -gt 0 ]]; then
    log "⚠ inactive after restart: ${inactive[*]}"
    log "  check: journalctl -u <unit> -n 50"
    exit 2
fi
log "✓ update complete: ${#to_restart[@]} daemons restarted, all active"
