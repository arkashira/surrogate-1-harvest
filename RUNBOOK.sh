#!/bin/bash
# axentx pipeline RUNBOOK — recreate full setup on fresh VM
# Usage: bash RUNBOOK.sh [--check|--install|--start|--all]
set -e

REPO=/opt/surrogate-1-harvest
SYSTEMD_SRC="$REPO/systemd"
DROPIN_SRC="$REPO/systemd-dropins"
ENV_FILE=/etc/surrogate-coordinator.env

check() {
  echo "=== Pre-flight check ==="
  [ -d "$REPO" ] || { echo "FAIL: $REPO not found"; return 1; }
  [ -f "$REPO/.venv/bin/python" ] || echo "WARN: venv not built ($REPO/.venv)"
  [ -f "$ENV_FILE" ] || echo "WARN: env file missing ($ENV_FILE) — secrets must be added"
  [ -d "$SYSTEMD_SRC" ] || { echo "FAIL: no systemd source dir"; return 1; }
  echo "  unit files in repo: $(ls $SYSTEMD_SRC/*.service 2>/dev/null | wc -l)"
  echo "  unit files in /etc: $(ls /etc/systemd/system/axentx-*.service 2>/dev/null | wc -l)"
  echo "  active daemons: $(systemctl list-units --state=running 'axentx-*' --no-legend --no-pager 2>/dev/null | wc -l)"
}

install() {
  echo "=== Install ALL unit files + drop-ins ==="
  sudo cp -v "$SYSTEMD_SRC"/*.service /etc/systemd/system/
  [ -d "$DROPIN_SRC" ] && sudo cp -rv "$DROPIN_SRC"/* /etc/systemd/system/ 2>/dev/null || true
  sudo systemctl daemon-reload
  echo "  installed: $(ls /etc/systemd/system/axentx-*.service | wc -l) units"
}

start() {
  echo "=== Enable + start ALL non-template axentx units ==="
  for unit in /etc/systemd/system/axentx-*.service; do
    name=$(basename "$unit")
    # Skip templates (@.service)
    [[ "$name" == *"@.service" ]] && continue
    sudo systemctl enable --now "$name" 2>&1 | grep -v "Created symlink" | tail -1 || true
  done
  echo "=== Start template instances (1-4 per template by default) ==="
  for tmpl in /etc/systemd/system/axentx-*@.service; do
    base=$(basename "$tmpl" | sed 's/@\.service$//')
    # Read template scale from comment if present, default 4
    SCALE=4
    case "$base" in
      axentx-dev-daemon) SCALE=80 ;;
      axentx-business-daemon) SCALE=8 ;;
      axentx-qa-daemon) SCALE=8 ;;
      axentx-reviewer-daemon) SCALE=8 ;;
    esac
    for i in $(seq 1 $SCALE); do
      sudo systemctl enable --now "${base}@${i}.service" 2>&1 >/dev/null || true
    done
  done
}

case "${1:-check}" in
  check)   check ;;
  install) install ;;
  start)   start ;;
  all)     check && install && start ;;
  *) echo "Usage: $0 [check|install|start|all]"; exit 1 ;;
esac
