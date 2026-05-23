#!/bin/bash
# axentx-disk-protector — runs every 30min via systemd timer.
# Prevents disk full by cleaning stale queue files + rotating logs.
set +e
LOG=/var/log/axentx-disk-protector.log
echo "[$(date -u +%FT%TZ)] disk-protector start" >> "$LOG"

USED_PCT=$(df /opt | awk "NR==2 {gsub(\"%\",\"\"); print \$5}")
echo "  disk used: ${USED_PCT}%" >> "$LOG"

# Always: clean stale claims + old done items
find /opt/surrogate-1-harvest/state/swarm-shared -name "*.claimed-*" -mmin +60 -delete 2>/dev/null
find /opt/surrogate-1-harvest/queue -name "*.claimed-*" -mmin +60 -delete 2>/dev/null

# Soft: truncate logs >100M
for f in /opt/surrogate-1-harvest/logs/*.log; do
  [ -f "$f" ] || continue
  sz=$(stat -c%s "$f" 2>/dev/null)
  if [ "${sz:-0}" -gt 104857600 ]; then
    tail -c 10485760 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
    echo "  truncated $f (was $((sz/1024/1024))M)" >> "$LOG"
  fi
done

# Aggressive: if disk >75%, clean done queue + old shard items
if [ "$USED_PCT" -gt 75 ]; then
  echo "  disk >75% — aggressive cleanup" >> "$LOG"
  # Old done items
  find /opt/surrogate-1-harvest/state/swarm-shared/done -name "*.json" -mmin +1440 -delete 2>/dev/null
  # Stale validator-queue items (queue rolls fast — anything >6h is dead)
  find /opt/surrogate-1-harvest/state/swarm-shared/validator-queue -name "*.json" -mmin +360 -delete 2>/dev/null
  find /opt/surrogate-1-harvest/state/swarm-shared/research-queue -name "*.json" -mmin +360 -delete 2>/dev/null
  find /opt/surrogate-1-harvest/state/swarm-shared/trend-raw-queue -name "*.json" -mmin +720 -delete 2>/dev/null
  # Old push-slices
  find /root/.surrogate/.push-slices -name "*.jsonl" -mtime +2 -delete 2>/dev/null
  # /tmp clutter
  find /tmp -maxdepth 1 -name "external-kb" -type d -mtime +0 -exec rm -rf {} + 2>/dev/null
fi

# Emergency: if disk >90%, vacuum journal too
if [ "$USED_PCT" -gt 90 ]; then
  echo "  disk >90% EMERGENCY" >> "$LOG"
  journalctl --vacuum-size=100M >> "$LOG" 2>&1
fi

# Clean stale git locks (>30min)
find /opt/axentx/*/.git -name "*.lock" -mmin +30 -delete 2>/dev/null
find /opt/axentx/*/.git -name "gc.log.lock" -delete 2>/dev/null

AFTER_PCT=$(df /opt | awk "NR==2 {gsub(\"%\",\"\"); print \$5}")
echo "  disk after: ${AFTER_PCT}%" >> "$LOG"
