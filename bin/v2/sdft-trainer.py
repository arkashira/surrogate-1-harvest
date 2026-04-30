"""Surrogate-1 v2 — SDFT (Self-Distillation Fine-Tuning) trainer.

Reference: arxiv.org/abs/2601.19897 (Yang et al. 2026)
Goal: continual LoRA training without catastrophic forgetting.

Core idea: instead of teaching the model with raw demonstrations, we
generate ON-POLICY responses from the model itself first, then distill
the demonstration's intent into that on-policy response. The training
distribution stays close to the model's current distribution → much less
forgetting of prior capabilities.

Pipeline (per training example {prompt, gold_response}):
  1. M_t generates a candidate response y_hat from prompt.
  2. Build a "distillation prompt": (prompt, y_hat, gold_response, "Combine
     the strengths of both"). A teacher M_distill rewrites y_hat to match
     gold_response intent while keeping y_hat's stylistic distribution.
  3. Train M_t on (prompt → distilled_response) with standard SFT loss.

We use the FREE LLM ladder as M_distill (no teacher model required) and
the current Surrogate checkpoint (or vLLM endpoint) as M_t.

Output: ~/.surrogate/data/v2/sdft/{stage}-{date}.jsonl ready for axolotl
SFT (stage1-sdft.yml) on next training run.

Run:
  python3 sdft-trainer.py --input gold.jsonl --stage stage1 --max 5000
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from sanitize import filter_pair  # type: ignore
except Exception:
    def filter_pair(p, r): return {"keep": True}

OUT_DIR = Path.home() / ".surrogate/data/v2/sdft"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SURROGATE_URL = os.environ.get("SURROGATE_URL", "http://127.0.0.1:8000")


def llm_ladder(prompt: str, sys_prompt: str = "",
               max_tokens: int = 1500, temperature: float = 0.5) -> str:
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


def surrogate_generate(prompt: str, max_tokens: int = 1024) -> str:
    """Step 1: M_t produces on-policy candidate y_hat."""
    try:
        req = json.dumps({
            "model": "surrogate-1-coder-7b-v2",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.7,
        }).encode()
        r = urllib.request.Request(
            f"{SURROGATE_URL}/v1/chat/completions", data=req,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=90) as resp:
            d = json.loads(resp.read())
            return d["choices"][0]["message"]["content"]
    except Exception:
        # Fallback: Qwen2.5-Coder-7B base via openrouter free
        return llm_ladder(prompt, "", max_tokens=max_tokens, temperature=0.7)


def distill(prompt: str, y_hat: str, gold: str) -> str:
    """Step 2: M_distill merges intent of gold into style/structure of y_hat."""
    sys_p = ("You are a distillation teacher. Rewrite the candidate response "
             "so that it captures all correct content from the gold reference, "
             "but keeps the candidate's natural phrasing, structure, and code "
             "style. Preserve any correct elements of the candidate. Do NOT "
             "copy gold verbatim. Output only the final response — no "
             "preamble, no markdown around the response.")
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\n"
              f"CANDIDATE (model's on-policy response):\n{y_hat[:3000]}\n\n"
              f"GOLD (reference answer):\n{gold[:3000]}\n\n"
              f"Rewrite candidate to match gold's correctness while keeping "
              f"candidate's style. Output only the rewritten response.")
    return llm_ladder(user_p, sys_p, max_tokens=1500, temperature=0.3)


def process(prompt: str, gold: str) -> dict | None:
    if not prompt or not gold or len(prompt) < 30 or len(gold) < 30:
        return None
    y_hat = surrogate_generate(prompt)
    if not y_hat or len(y_hat) < 30:
        return None
    distilled = distill(prompt, y_hat, gold)
    if not distilled or len(distilled) < 50:
        return None
    if not filter_pair(prompt, distilled)["keep"]:
        return None
    return {
        "prompt": prompt[:100000],
        "response": distilled[:200000],
        "source": "sdft",
        "meta": {
            "y_hat_len": len(y_hat),
            "gold_len": len(gold),
            "distilled_len": len(distilled),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="JSONL with {prompt, response} (gold) per line")
    ap.add_argument("--stage", default="stage1",
                    help="output filename prefix")
    ap.add_argument("--max", type=int, default=5000)
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        print(f"❌ {inp} missing", file=sys.stderr)
        sys.exit(1)

    out = OUT_DIR / f"{args.stage}-{time.strftime('%Y%m%d')}.jsonl"
    n_in = 0
    n_kept = 0
    with open(inp) as fin, open(out, "a") as fout:
        for line in fin:
            if n_kept >= args.max:
                break
            try:
                d = json.loads(line)
            except Exception:
                continue
            n_in += 1
            prompt = d.get("prompt") or d.get("instruction") or ""
            gold = (d.get("response") or d.get("output")
                    or d.get("answer") or "")
            if (not prompt or not gold) and isinstance(d.get("messages"), list):
                msgs = d["messages"]
                u = next((m.get("content", "") for m in msgs
                         if m.get("role") in ("user", "human")), "")
                a = next((m.get("content", "") for m in msgs
                         if m.get("role") in ("assistant", "gpt")), "")
                if u and a:
                    prompt, gold = u, a
            row = process(prompt, gold)
            if row:
                fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                fout.flush()
                n_kept += 1
                if n_kept % 50 == 0:
                    print(f"  sdft kept {n_kept}/{args.max} (in {n_in})")
    print(f"[done] in={n_in} sdft_kept={n_kept} → {out}")


if __name__ == "__main__":
    main()
