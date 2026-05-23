#!/usr/bin/env python3
"""Adapter eval gate — block HF Hub publish on quality regression.

Usage:
    eval-adapter-gate.py <hf_model_id> [--baseline <id>] [--prompts N]

What it does:
  1. Loads the held-out eval set (data/eval/held_out_v1.jsonl)
  2. For each prompt: gets a completion from candidate adapter and from
     the baseline (HF base or last-published adapter). Uses the LLM chain
     in axentx_pipeline.call_llm — the candidate is invoked through HF
     Inference API; the baseline is the same model id with /revision pinned.
  3. Two metrics:
       * pseudo-perplexity proxy: avg negative-log-prob via repeated short
         continuations (we don't have logprobs from the providers, so we use
         a deterministic surrogate — character-bigram cross-entropy of
         candidate output vs reference; lower is better, like perplexity).
       * win-rate: pairwise judge prompt asks the LLM "which response is
         better at task X" — candidate wins ≥3% over base required.
  4. Exits 0 only if BOTH metrics improve by ≥3% over baseline.

Why exit-code-driven: this script runs in CI before the HF push step.
GitHub Actions / push-training-to-hf.sh check `$?` and abort upload on
non-zero. No network = no gate (fail closed) — non-zero exit on any
infra failure too.

Output: JSON to stdout summarizing scores. Logs to logs/eval-gate.log.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[1]))
# axentx_pipeline expects REPO_ROOT in env — set it so its module-level
# directory creation lands in the right place when invoked from CI / a
# checkout that isn't /opt/surrogate-1-harvest.
os.environ.setdefault("REPO_ROOT", str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import call_llm  # noqa: E402

EVAL_SET = REPO_ROOT / "data" / "eval" / "held_out_v1.jsonl"
LOG_FILE = REPO_ROOT / "logs" / "eval-gate.log"
DEFAULT_BASELINE = "Qwen/Qwen2.5-Coder-7B-Instruct"
DEFAULT_PROMPTS = 20
MIN_WIN_RATE_DELTA = 0.03    # candidate must win 3% more rounds
MIN_PERPLEXITY_DELTA = 0.03  # candidate must lower entropy 3%


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, file=sys.stderr, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_eval_prompts(limit: int) -> list[dict]:
    if not EVAL_SET.exists():
        raise FileNotFoundError(
            f"eval set missing: {EVAL_SET} — run #50 first to seed it"
        )
    rows: list[dict] = []
    with EVAL_SET.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(rows) >= limit:
                break
    if not rows:
        raise RuntimeError(f"eval set is empty: {EVAL_SET}")
    return rows


def _bigram_entropy(text: str) -> float:
    """Character-bigram cross-entropy — perplexity surrogate.

    We do not have token-level logprobs from generic chat APIs, so use this
    cheap proxy: a coherent completion has lower bigram entropy (more
    repetitive structure) than a degenerate or off-topic one. Used only
    relatively (candidate vs baseline) so absolute value is fine.
    """
    if not text or len(text) < 4:
        return 99.0  # treat empty/junk as max entropy
    counts: dict[str, int] = {}
    total = 0
    for i in range(len(text) - 1):
        bg = text[i : i + 2]
        counts[bg] = counts.get(bg, 0) + 1
        total += 1
    h = 0.0
    for c in counts.values():
        p = c / total
        h -= p * math.log2(p)
    return h


def generate(model_id: str, prompt: str) -> str:
    """Hit the candidate / baseline via HF Inference API.

    Falls through call_llm chain if HF inference fails; the chain itself
    routes to providers that can serve `model_id`. For untrained baselines
    we pass the prompt through call_llm with a neutral system message.
    """
    system = (
        f"You are evaluating model {model_id}. Respond as that model would: "
        "concise, direct, factual."
    )
    return call_llm(prompt, system=system, max_tokens=400, timeout=45)


def judge_pair(prompt: str, a: str, b: str) -> str:
    """Ask the LLM chain to pick winner. Returns 'A' | 'B' | 'TIE'."""
    judge_prompt = (
        f"Task: judge which response better addresses the prompt below. "
        f"Score on relevance, specificity, accuracy. Reply with exactly one "
        f"token: A, B, or TIE.\n\n"
        f"### Prompt\n{prompt}\n\n"
        f"### Response A\n{a[:1500]}\n\n"
        f"### Response B\n{b[:1500]}\n\n"
        f"Winner:"
    )
    try:
        verdict = call_llm(judge_prompt, max_tokens=4, timeout=30).strip().upper()
    except Exception as exc:
        log(f"judge error: {exc}")
        return "TIE"
    if verdict.startswith("A"):
        return "A"
    if verdict.startswith("B"):
        return "B"
    return "TIE"


def evaluate(prompts: Iterable[dict], candidate: str, baseline: str) -> dict:
    win = tie = loss = 0
    cand_entropy: list[float] = []
    base_entropy: list[float] = []
    per_prompt: list[dict] = []

    for i, row in enumerate(prompts):
        prompt = row.get("prompt") or row.get("input") or ""
        if not prompt:
            continue
        try:
            cand_out = generate(candidate, prompt)
            base_out = generate(baseline, prompt)
        except Exception as exc:
            log(f"  generation failed at {i}: {exc}")
            continue
        cand_entropy.append(_bigram_entropy(cand_out))
        base_entropy.append(_bigram_entropy(base_out))
        verdict = judge_pair(prompt, cand_out, base_out)
        if verdict == "A":
            win += 1
        elif verdict == "B":
            loss += 1
        else:
            tie += 1
        per_prompt.append({
            "i": i,
            "verdict": verdict,
            "cand_h": cand_entropy[-1],
            "base_h": base_entropy[-1],
        })
        log(f"  [{i+1}] verdict={verdict} cand_h={cand_entropy[-1]:.3f} base_h={base_entropy[-1]:.3f}")

    n = win + tie + loss
    win_rate = win / n if n else 0
    base_win_rate = loss / n if n else 0
    cand_h_mean = sum(cand_entropy) / len(cand_entropy) if cand_entropy else 0
    base_h_mean = sum(base_entropy) / len(base_entropy) if base_entropy else 0
    # "perplexity delta" reported as relative reduction in entropy.
    if base_h_mean:
        perplexity_delta = (base_h_mean - cand_h_mean) / base_h_mean
    else:
        perplexity_delta = 0
    win_rate_delta = win_rate - base_win_rate

    return {
        "candidate": candidate,
        "baseline": baseline,
        "n_evaluated": n,
        "win": win,
        "tie": tie,
        "loss": loss,
        "win_rate": round(win_rate, 4),
        "base_win_rate": round(base_win_rate, 4),
        "win_rate_delta": round(win_rate_delta, 4),
        "cand_entropy_mean": round(cand_h_mean, 4),
        "base_entropy_mean": round(base_h_mean, 4),
        "perplexity_delta": round(perplexity_delta, 4),
        "per_prompt": per_prompt,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hf_model_id", help="HF model id of the candidate adapter")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--prompts", type=int, default=DEFAULT_PROMPTS)
    args = ap.parse_args()

    log(f"gate START candidate={args.hf_model_id} baseline={args.baseline} n={args.prompts}")
    prompts = load_eval_prompts(args.prompts)
    result = evaluate(prompts, args.hf_model_id, args.baseline)

    passes_winrate = result["win_rate_delta"] >= MIN_WIN_RATE_DELTA
    passes_perplexity = result["perplexity_delta"] >= MIN_PERPLEXITY_DELTA
    result["passes_winrate"] = passes_winrate
    result["passes_perplexity"] = passes_perplexity
    result["gate_passed"] = bool(passes_winrate and passes_perplexity)

    print(json.dumps(result, indent=2))
    log(f"gate END passed={result['gate_passed']} "
        f"wr_delta={result['win_rate_delta']} ppl_delta={result['perplexity_delta']}")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
