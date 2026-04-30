"""Surrogate-1 v2 — Self-Refine 3-iter loop.

Reference: Madaan et al. 2023 (Self-Refine). 3-iteration generate→critique→
revise loop.

Diff vs constitutional-loop.py:
  • constitutional-loop = ONE pass with 8 fixed principles → DPO triple
  • self-refine        = THREE iterations of free-form critique → final SFT

Useful for high-stakes outputs where additional refinement compounds
quality. Output schema = SFT (chosen-only), not DPO. Plug into stage1
training mix or stage1.5 polish stage.

CLI:
  python3 self-refine-loop.py --input prompts.jsonl --n 200
  → /data/v2/self-refine-sft.jsonl
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from sanitize import filter_pair  # type: ignore
except Exception:
    def filter_pair(p, r): return {"keep": True}

OUT_PATH = Path.home() / ".surrogate/data/v2/self-refine-sft.jsonl"
ITERATIONS = 3


def llm_ladder(prompt: str, sys_prompt: str = "",
               max_tokens: int = 1200, temperature: float = 0.4) -> str:
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


def initial_answer(prompt: str) -> str:
    sys_p = ("You are Surrogate-1, an expert DevSecOps + SRE + coding agent. "
             "Answer the prompt with production-quality code/config.")
    return llm_ladder(prompt, sys_p, max_tokens=1500, temperature=0.5)


def critique(prompt: str, answer: str, iter_n: int) -> str:
    sys_p = ("You are a senior reviewer. Critique the answer for: "
             "correctness, security, completeness, idiomatic style, missing "
             "edge cases. Output 3-5 specific actionable improvements (no "
             "praise, no hedging). If nothing to improve, output 'NONE'.")
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\nANSWER (iteration {iter_n}):\n"
              f"{answer[:3000]}\n\nList specific improvements, "
              f"or 'NONE' if perfect.")
    return llm_ladder(user_p, sys_p, max_tokens=400, temperature=0.2)


def refine(prompt: str, answer: str, critique_text: str) -> str:
    if critique_text.strip().upper().startswith("NONE"):
        return answer  # converged
    sys_p = ("You are Surrogate-1. Apply the listed improvements to the "
             "answer. Keep what's already correct. Output ONLY the revised "
             "answer — no preamble, no markdown around the answer block.")
    user_p = (f"PROMPT:\n{prompt[:1500]}\n\nCURRENT ANSWER:\n{answer[:3000]}\n\n"
              f"IMPROVEMENTS TO APPLY:\n{critique_text[:1500]}\n\n"
              f"Output the revised answer.")
    return llm_ladder(user_p, sys_p, max_tokens=1500, temperature=0.3)


def process(prompt: str) -> dict | None:
    if len(prompt) < 30:
        return None
    answer = initial_answer(prompt)
    if not answer:
        return None

    history = [answer]
    for i in range(1, ITERATIONS + 1):
        crit = critique(prompt, answer, i)
        if not crit or crit.strip().upper().startswith("NONE"):
            break
        revised = refine(prompt, answer, crit)
        if not revised or revised.strip() == answer.strip():
            break
        history.append(revised)
        answer = revised

    if not filter_pair(prompt, answer)["keep"]:
        return None

    return {
        "prompt": prompt[:100000],
        "response": answer[:200000],
        "source": "self-refine",
        "meta": {
            "iterations_used": len(history),
            "first_draft_len": len(history[0]),
            "final_len": len(answer),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="JSONL with {prompt} per line")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not inp.exists():
        print(f"❌ {inp} missing", file=sys.stderr); sys.exit(1)

    n_in = n_out = 0
    with open(inp) as fin, open(out, "a") as fout:
        for line in fin:
            if n_out >= args.n:
                break
            try:
                d = json.loads(line)
            except Exception:
                continue
            n_in += 1
            prompt = d.get("prompt") or d.get("instruction") or ""
            row = process(prompt)
            if row:
                fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                fout.flush()
                n_out += 1
                if n_out % 25 == 0:
                    print(f"  refined {n_out}/{args.n} (in {n_in})")
    print(f"[done] in={n_in} kept={n_out} → {out}")


if __name__ == "__main__":
    main()
