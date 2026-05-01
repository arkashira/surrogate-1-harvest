#!/usr/bin/env python3
"""hermes-worker — continuous daemon, drains tasks-queue/pending/.

Multiple workers run in parallel (axentx-worker-1, axentx-worker-2, etc.),
each polls pending/ atomically (rename-based race-safe claim), executes
shell or LLM-prompt job, moves to done/ or failed/.

This replaces the old cron dispatcher's per-tick batch model with truly
overlapping continuous work.

Identifies itself by WORKER_ID env var (set by systemd unit).
"""
from __future__ import annotations

import gc as gc_module
import json
import os
import resource
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hermes_workqueue import (REPO_ROOT, log, claim_oldest_pending, finish_task)
# Re-use the canonical 11-provider chain
from axentx_pipeline import call_llm

WORKER_ID = os.environ.get("WORKER_ID", "default")
POLL_SEC = int(os.environ.get("WORKER_POLL_SEC", "5"))
SOFT_RSS_KB = int(os.environ.get("WORKER_SOFT_RSS_KB", "49152"))
BIN_DIR = REPO_ROOT / "bin"


def shutdown(*_):
    log(f"worker-{WORKER_ID}", "shutdown")
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def _rewrite_cmd(cmd: str) -> str:
    """Same Mac→cloud path rewrites as the old dispatcher."""
    rewrites = (
        ("/Users/Ashira/.claude/bin/", str(BIN_DIR) + "/"),
        ("/Users/Ashira/.hermes/scripts/", str(BIN_DIR) + "/"),
        ("/Users/Ashira/.hermes/bin/", str(BIN_DIR) + "/"),
        ("/Users/Ashira/.surrogate/bin/", str(BIN_DIR) + "/"),
        ("$HOME/.claude/bin/", str(BIN_DIR) + "/"),
        ("$HOME/.hermes/scripts/", str(BIN_DIR) + "/"),
        ("~/.claude/bin/", str(BIN_DIR) + "/"),
        ("~/.hermes/scripts/", str(BIN_DIR) + "/"),
    )
    for old, new in rewrites:
        cmd = cmd.replace(old, new)
    return cmd


def execute(item: dict) -> tuple[bool, str]:
    script = item.get("script")
    if script:
        cmd = _rewrite_cmd(script)
        env = dict(os.environ)
        env["PATH"] = f"{BIN_DIR}:{env.get('PATH', '')}"
        try:
            r = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True,
                timeout=30, env=env, cwd=str(REPO_ROOT),
            )
            return r.returncode == 0, (r.stdout + r.stderr)[:4000]
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT after 30s"
        except Exception as e:
            return False, f"EXEC ERROR: {type(e).__name__}: {e}"
    prompt = item.get("prompt", "")
    if not prompt:
        return False, "no script or prompt"
    try:
        out = call_llm(prompt, max_tokens=1024, timeout=20)
        return True, out[:4000]
    except Exception as e:
        return False, f"LLM call failed: {e}"


log(f"worker-{WORKER_ID}", f"start — poll every {POLL_SEC}s, RSS soft cap {SOFT_RSS_KB} KB")
n_processed = 0
while True:
    try:
        claim = claim_oldest_pending()
        if claim is None:
            time.sleep(POLL_SEC)
            continue
        running_path, item = claim
        log(f"worker-{WORKER_ID}", f"▸ {item.get('id')}  ({item.get('name','?')})")
        ok, output = execute(item)
        finish_task(running_path, item, ok, output)
        n_processed += 1
        if ok:
            log(f"worker-{WORKER_ID}", f"✓ {item.get('id')}  ({len(output)} chars)")
        else:
            log(f"worker-{WORKER_ID}", f"✗ {item.get('id')}  {output[:150]}")

        # OOM-safe: explicit GC + RSS check + graceful exit if approaching cap
        gc_module.collect()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss > SOFT_RSS_KB:
            log(f"worker-{WORKER_ID}", f"RSS {rss} KB > soft cap — graceful restart")
            sys.exit(0)
    except Exception as e:
        log(f"worker-{WORKER_ID}", f"⚠ loop exception: {type(e).__name__}: {e}")
        time.sleep(POLL_SEC)
