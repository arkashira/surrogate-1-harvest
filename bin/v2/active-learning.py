"""Surrogate-1 v2 — Active learning by uncertainty sampling.

For the next training batch, we want the highest-leverage examples:
ones the current Surrogate is most UNCERTAIN about. Those teach more per
gradient step than easy ones.

Approach (no logprobs available from free LLM bridges):
  1. Pull a candidate pool from one of the bulk-mirror JSONLs.
  2. Surrogate generates 3 completions per prompt at temperature 0.7.
  3. Pairwise similarity (Jaccard on token sets) → variance score.
  4. High variance = high uncertainty → keep for labeling.
  5. Send keepers to LLM-judge ladder for canonical answer.
  6. Append to ~/.surrogate/data/v2/active-learning-batch.jsonl

Run: python3 active-learning.py --pool /path/to.jsonl --n 200
"""
from __future__ import annotations
import argparse
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from sanitize import filter_pair  # type: ignore
    from dedup import DedupStore       # type: ignore
    HAS_DEDUP = True
except Exception:
    def filter_pair(p, r): return {"keep": True}
    HAS_DEDUP = False

OUT_PATH = Path.home() / ".surrogate/data/v2/active-learning-batch.jsonl"
SURROGATE_URL = os.environ.get("SURROGATE_URL", "http://127.0.0.1:8000")
TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _toks(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _llm_ladder(prompt: str, sys_prompt: str = "",
                max_tokens: int = 1024, temperature: float = 0.7) -> str:
    bridges = [
        "$HOME/.surrogate/bin/cerebras-bridge.sh",
        "$HOME/.surrogate/bin/groq-bridge.sh",
        "$HOME/.surrogate/bin/openrouter-bridge.sh",
        "$HOME/.surrogate/bin/gemini-bridge.sh",
        # "$HOME/.surrogate/bin/chutes-bridge.sh",  # disabled 2026-04-30: chutes 402 free-tier dead
        "$HOME/.surrogate/bin/ollama-bridge.sh",
    ]
    for sh in bridges:
        sh_path = os.path.expandvars(sh)
        if not Path(sh_path).exists():
            continue
        try:
            req = json.dumps({"system": sys_prompt, "prompt": prompt,
                              "max_tokens": max_tokens,
                              "temperature": temperature})
            r = subprocess.run(["bash", sh_path], input=req,
                               capture_output=True, text=True, timeout=60)
            out = (r.stdout or "").strip()
            if out and len(out) > 30:
                return out
        except Exception:
            continue
    return ""


def _surrogate_sample(prompt: str, n: int = 3,
                      temperature: float = 0.7) -> list[str]:
    """Try local vLLM endpoint first, else fall back to ladder with shuffled order."""
    out = []
    try:
        req = json.dumps({
            "model": "surrogate-1-coder-7b-v2",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 768, "temperature": temperature, "n": n,
        }).encode()
        r = urllib.request.Request(
            f"{SURROGATE_URL}/v1/chat/completions", data=req,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=90) as resp:
            d = json.loads(resp.read())
            for ch in d.get("choices", []):
                t = ch.get("message", {}).get("content", "").strip()
                if t:
                    out.append(t)
    except Exception:
        pass
    while len(out) < n:
        c = _llm_ladder(prompt, "You are Surrogate-1, an expert coding agent.",
                        max_tokens=768, temperature=temperature)
        if not c:
            break
        out.append(c)
    return out


def _uncertainty(samples: list[str]) -> float:
    """Mean pairwise Jaccard distance. Higher = more disagreement = more uncertain."""
    if len(samples) < 2:
        return 0.0
    sets = [_toks(s) for s in samples]
    sims = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            sims.append(_jaccard(sets[i], sets[j]))
    if not sims:
        return 0.0
    mean_sim = statistics.mean(sims)
    return 1.0 - mean_sim


def _judge_label(prompt: str, candidates: list[str]) -> str:
    sys_p = ("You are an expert reviewer. Given the prompt and candidate "
             "answers, output the BEST canonical answer. Combine the best "
             "parts if useful. Output only the final answer — no preamble.")
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\nCANDIDATES:\n" +
              "\n---\n".join(f"[{i+1}] {c[:1500]}"
                              for i, c in enumerate(candidates)) +
              "\n\nReturn the best canonical answer.")
    return _llm_ladder(user_p, sys_p, max_tokens=1500, temperature=0.2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True,
                    help="JSONL with {prompt} per line")
    ap.add_argument("--n", type=int, default=200,
                    help="how many high-uncertainty examples to keep")
    ap.add_argument("--scan", type=int, default=2000,
                    help="how many pool entries to evaluate")
    ap.add_argument("--threshold", type=float, default=0.4,
                    help="min uncertainty to keep")
    args = ap.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.exists():
        print(f"❌ pool not found: {pool_path}", file=sys.stderr)
        sys.exit(1)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    candidates: list[tuple[float, str, list[str]]] = []
    seen_count = 0

    with open(pool_path) as f:
        lines = f.readlines()
        random.shuffle(lines)
        for line in lines[:args.scan]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            prompt = (d.get("prompt") or d.get("instruction")
                      or d.get("input") or "")[:3000]
            if len(prompt) < 30:
                continue
            samples = _surrogate_sample(prompt, n=3)
            if len(samples) < 2:
                continue
            u = _uncertainty(samples)
            seen_count += 1
            if u >= args.threshold:
                candidates.append((u, prompt, samples))
            if (seen_count) % 25 == 0:
                print(f"  scanned {seen_count} kept {len(candidates)}")

    # Top by uncertainty
    candidates.sort(key=lambda x: -x[0])
    keep = candidates[:args.n]
    print(f"[label] LLM-judging {len(keep)} candidates")

    n_written = 0
    with open(OUT_PATH, "a") as fout:
        for u, prompt, samples in keep:
            label = _judge_label(prompt, samples)
            if not label or len(label) < 30:
                continue
            if not filter_pair(prompt, label)["keep"]:
                continue
            if HAS_DEDUP and not DedupStore.is_new(prompt, source="active-learning"):
                continue
            fout.write(json.dumps({
                "prompt": prompt, "response": label,
                "source": "active-learning",
                "meta": {"uncertainty": round(u, 3),
                         "n_candidates": len(samples)},
            }, ensure_ascii=False) + "\n")
            n_written += 1

    print(f"[done] scanned={seen_count} high_uncertainty={len(keep)} "
          f"labeled+kept={n_written} → {OUT_PATH}")


if __name__ == "__main__":
    main()
