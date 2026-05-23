#!/usr/bin/env python3
"""axentx v1 warm-up — keep Surrogate-1 v1 HF Space hot so fallback works.

User directive (2026-05-02):
  > 'ให้ตายยังไงมันก็มี model ทำงานได้'
  > 'ทำไมยังติดปัญหา 429 อยู่อีก ในเมื่อมี fallback ตั้งหลายชั้น'

Why this exists:
  HF ZeroGPU Spaces sleep after ~48h idle (resp. 30 min if no GPU access
  pending). When the entire LLM chain is rate-limited, the pipeline
  falls through to surrogate-1 v1 — but v1 is on a sleeping Space, so
  the first call cold-boots Chromium runtime + LoRA load (15-30s) which
  then times out, treating v1 as 'failed' and bouncing the cycle.

  This daemon pings v1 every WARMUP_SEC (default 10 min) with a
  trivial prompt to keep it warm. Cost: ~6 / hour minimal-token calls,
  well within free-tier ZeroGPU quota.

  When other providers fail and call_llm falls through to v1, the Space
  is already warm → response in 2-5s instead of timeout.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import _call_surrogate_v1, log, daemon_loop  # noqa: E402

WARMUP_SEC = int(os.environ.get("V1_WARMUP_SEC", "600"))   # 10 min
WARMUP_PROMPT = "Reply with the single word: OK"


def do_one() -> bool:
    t0 = time.monotonic()
    try:
        out = _call_surrogate_v1(WARMUP_PROMPT, timeout=45)
        elapsed = time.monotonic() - t0
        log("v1-warmup", f"✓ warm ({elapsed:.1f}s) — {(out or '')[:60]!r}")
        return True
    except Exception as e:
        elapsed = time.monotonic() - t0
        log("v1-warmup", f"✗ {elapsed:.1f}s {type(e).__name__}: {str(e)[:120]}")
        return False


if __name__ == "__main__":
    daemon_loop("v1-warmup", WARMUP_SEC, do_one)
