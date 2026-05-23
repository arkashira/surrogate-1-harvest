"""Surrogate-1 v2 — Teachable-prompt filter (Phi-4-Reasoning).

Reference: Microsoft Phi-4-Reasoning Tech Report (2025).

Filter SFT prompts to those where the BASE Surrogate scores roughly 50%
accuracy. Easy prompts reinforce existing patterns (no learning).
Impossibly-hard prompts have no learning signal (gradient noise).
Sweet spot = 30-70% baseline accuracy.

Token-efficient SFT: train on prompts the model is most able to learn
from, skip the rest. Phi-4-Reasoning showed strong gains on 8.3B "right
level of complexity" tokens vs full corpus.

Usage:
  python3 teachable-prompt-filter.py \
      --input candidate-prompts.jsonl \
      --baseline-url http://127.0.0.1:8000 \
      --n 5000 \
      --out filtered.jsonl
"""
from __future__ import annotations
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
TARGET_LO = float(os.environ.get("TEACHABLE_LO", 0.30))
TARGET_HI = float(os.environ.get("TEACHABLE_HI", 0.70))
N_SAMPLES = int(os.environ.get("TEACHABLE_N_SAMPLES", 3))


def llm_ladder(prompt: str, sys_prompt: str = "",
               max_tokens: int = 1024, temperature: float = 0.7) -> str:
    bridges = [
        "$HOME/.surrogate/bin/zero-gpu-bridge.sh",
        "$HOME/.surrogate/bin/cerebras-bridge.sh",
        "$HOME/.surrogate/bin/groq-bridge.sh",
        "$HOME/.surrogate/bin/hf-inference-bridge.sh",
        "$HOME/.surrogate/bin/gemini-bridge.sh",
        "$HOME/.surrogate/bin/openrouter-bridge.sh",
        # "$HOME/.surrogate/bin/chutes-bridge.sh",  # disabled 2026-04-30: chutes 402 free-tier dead
    ]
    for sh in bridges:
        sh_path = os.path.expandvars(sh)
        if not Path(sh_path).exists():
            continue
        try:
            full = (sys_prompt + "\n\n" + prompt).strip() if sys_prompt else prompt
            r = subprocess.run(["bash", sh_path, "--max-tokens", str(max_tokens)],
                               input=full, capture_output=True, text=True,
                               timeout=90)
            out = (r.stdout or "").strip()
            if out and len(out) > 10:
                return out
        except Exception:
            continue
    return ""


def baseline_score(prompt: str, gold: str, n: int = N_SAMPLES) -> float:
    """Sample n responses from base model, score against gold.
    Returns 0.0-1.0 fraction of correct generations.
    """
    if not gold:
        return 0.5  # no gold → can't judge → treat as borderline
    n_correct = 0
    n_tries = 0
    sys_p = ("You are Qwen2.5-Coder-7B-Instruct (base). Answer concisely.")
    for _ in range(n):
        out = llm_ladder(prompt, sys_p, max_tokens=512, temperature=0.7)
        if not out:
            continue
        n_tries += 1
        if _is_correct(out, gold):
            n_correct += 1
    if n_tries == 0:
        return 0.5
    return n_correct / n_tries


def _is_correct(response: str, gold: str) -> bool:
    """Quick correctness check: substring OR last-number match."""
    g_norm = gold.strip().lower()
    r_norm = response.strip().lower()
    # Substring (gold short enough to be embeddable)
    if len(g_norm) < 200 and g_norm in r_norm:
        return True
    # Numeric gold
    g_nums = NUM_RE.findall(gold); r_nums = NUM_RE.findall(response)
    if g_nums and r_nums:
        try:
            return abs(float(g_nums[-1]) - float(r_nums[-1])) < 1e-3
        except ValueError:
            pass
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=2000,
                    help="max prompts to score (sample)")
    ap.add_argument("--keep-target", type=int, default=500,
                    help="how many teachable prompts to keep")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not inp.exists():
        print(f"❌ {inp} missing", file=sys.stderr); sys.exit(1)

    rows = []
    with open(inp) as f:
        for line in f:
            try: rows.append(json.loads(line))
            except: pass
    random.shuffle(rows)
    rows = rows[:args.n]
    print(f"[score] {len(rows)} candidate prompts")

    teachable = []
    too_easy = too_hard = 0
    with open(out, "w") as fout:
        for i, r in enumerate(rows):
            prompt = r.get("prompt") or r.get("instruction") or ""
            gold = (r.get("response") or r.get("answer") or r.get("output") or "")
            if not prompt or not gold:
                continue
            score = baseline_score(prompt, gold)
            r["teachable"] = {"baseline_score": round(score, 3),
                              "kept": TARGET_LO <= score <= TARGET_HI}
            if r["teachable"]["kept"]:
                teachable.append(r)
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
                fout.flush()
            elif score < TARGET_LO:
                too_hard += 1
            else:
                too_easy += 1
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(rows)} kept={len(teachable)} "
                      f"easy={too_easy} hard={too_hard}")
            if len(teachable) >= args.keep_target:
                break
    print(f"[done] kept={len(teachable)} too_easy={too_easy} too_hard={too_hard}")


if __name__ == "__main__":
    main()
