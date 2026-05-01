#!/usr/bin/env bash
# OOM hardening for OCI E2.1.Micro 1 GB RAM.
# Run on the coordinator after bootstrap. Idempotent.
#
# Why: 5 Python daemons × synth-3 × LLM-history accumulate → OOM kill →
# sshd dies → 5h downtime. Fix root cause: tighter limits, swap, GC.
#
# Usage: ssh ubuntu@<coordinator> 'curl -sSL .../oci-oom-hardening.sh | sudo bash'
set -euo pipefail

echo "[hardening] starting"

# 1. Add 2 GB swap (instance has 1 GB RAM; swap saves us from OOM kill)
if [ ! -f /swapfile ]; then
    echo "[hardening] adding 2 GB swap"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    grep -q "/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
    sudo sysctl vm.swappiness=10  # only swap when really needed
    echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf
fi

# 2. Tighten per-daemon MemoryMax (was 128M each, total cap = 768M of 1GB)
# Reduce to 64M each, total 384M, leaves 600+ MB for OS + sshd + buffer
for s in axentx-dev-daemon axentx-reviewer-daemon axentx-qa-daemon \
         axentx-commit-daemon axentx-pm-daemon; do
    if [ -f "/etc/systemd/system/$s.service" ]; then
        sudo sed -i 's/MemoryMax=128M/MemoryMax=64M/' "/etc/systemd/system/$s.service"
        # Add MemoryHigh = soft limit, kicks in BEFORE Max = no hard kill
        if ! grep -q "MemoryHigh" "/etc/systemd/system/$s.service"; then
            sudo sed -i '/MemoryMax=/a MemoryHigh=48M' "/etc/systemd/system/$s.service"
        fi
        # OOMPolicy=continue: don't restart-storm on OOM, let next tick recover
        if ! grep -q "OOMPolicy" "/etc/systemd/system/$s.service"; then
            sudo sed -i '/RestartSec=/a OOMPolicy=continue' "/etc/systemd/system/$s.service"
        fi
    fi
done

# 3. Reserve memory for sshd — protect it from OOM killer (oom_score_adj=-1000)
sudo mkdir -p /etc/systemd/system/ssh.service.d
sudo tee /etc/systemd/system/ssh.service.d/oom-protect.conf >/dev/null <<EOF
[Service]
OOMScoreAdjust=-1000
EOF

# 4. Reload + restart all
sudo systemctl daemon-reload
sudo systemctl restart ssh
for s in axentx-dev-daemon axentx-reviewer-daemon axentx-qa-daemon \
         axentx-commit-daemon axentx-pm-daemon; do
    [ -f "/etc/systemd/system/$s.service" ] && sudo systemctl restart "$s"
done

echo "[hardening] ✓ done"
echo ""
echo "memory after:"
free -h
echo ""
echo "swap:"
swapon --show
