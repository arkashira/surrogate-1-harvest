"""Surrogate-1 v2 — TruthRL ternary rewarder.

Reference: TruthRL (2024) — instead of binary correct/wrong, reward CALIBRATED
abstention. Three outcomes:

  +1.0  correct + confident
   0.0  abstain ('I don't know', 'verify against docs') when actually uncertain
  -1.0  confident + wrong (hallucination)

This produces a model that says IDK on questions it would otherwise hallucinate.

Used in stage3-dapo.yml composite reward as the `truthrl` head:
  composite = 0.4*test_pass + 0.2*lint + 0.2*security
              + 0.2*truthrl   ← THIS

Inputs: (prompt, response, gold_or_judge_verdict). Output: ternary score.

Detects abstention with regex over response (fast, no LLM call). Detects
correctness via judge LLM (free ladder) only when not abstaining — saves cost.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ABSTAIN_PHRASES = re.compile(
    r"\b(?:i\s+don'?t\s+know|i'?m\s+not\s+(?:sure|certain)|"
    r"can(?:not|'?t)\s+verify|verify\s+(?:against|with)\s+(?:docs|the\s+docs|official)|"
    r"check\s+(?:the\s+)?(?:docs|documentation|with\s+the\s+vendor)|"
    r"would\s+need\s+to\s+(?:check|verify)|"
    r"unable\s+to\s+(?:confirm|determine)|"
    r"not\s+enough\s+(?:context|info)|need\s+more\s+context|"
    r"this\s+may\s+be\s+(?:out\s+of\s+date|outdated)|"
    r"please\s+confirm|please\s+verify)\b",
    re.IGNORECASE)

# Confident-claim signals — used to detect when model claims certainty
CONFIDENT_SIGNALS = re.compile(
    r"\b(?:certainly|definitely|always|never|guaranteed|absolutely|"
    r"is\s+the\s+case|the\s+answer\s+is|the\s+correct\s+(?:way|answer))\b",
    re.IGNORECASE)


def is_abstaining(response: str) -> bool:
    if not response:
        return False
    # Heuristic: must abstain in first 40% of response, not buried at end
    head = response[: max(200, len(response) // 2)]
    if not ABSTAIN_PHRASES.search(head):
        return False
    # If response ALSO has long confident-sounding code/answer block,
    # it's not really abstaining — it's hedging then answering anyway.
    body = response[len(head):]
    if CONFIDENT_SIGNALS.search(body) and len(body) > 200:
        return False
    return True


def llm_judge_correctness(prompt: str, response: str,
                          gold: str | None = None) -> dict:
    """Returns {'correct': bool, 'confidence': float, 'why': str}."""
    bridges = [
        "$HOME/.surrogate/bin/cerebras-bridge.sh",
        "$HOME/.surrogate/bin/groq-bridge.sh",
        "$HOME/.surrogate/bin/openrouter-bridge.sh",
        "$HOME/.surrogate/bin/gemini-bridge.sh",
        # "$HOME/.surrogate/bin/chutes-bridge.sh",  # disabled 2026-04-30: chutes 402 free-tier dead
        "$HOME/.surrogate/bin/ollama-bridge.sh",
    ]
    sys_p = ("You are a strict factual reviewer. Decide if the response is "
             "factually correct AND specific enough to be useful. Return ONLY "
             "JSON: {\"correct\": bool, \"confidence\": float in [0,1], "
             "\"why\": str}. No markdown.")
    if gold:
        user_p = (f"PROMPT:\n{prompt[:1500]}\n\nGOLD:\n{gold[:2000]}\n\n"
                  f"RESPONSE:\n{response[:3000]}\n\n"
                  f"Compare RESPONSE to GOLD. JSON only.")
    else:
        user_p = (f"PROMPT:\n{prompt[:1500]}\n\nRESPONSE:\n{response[:3000]}\n\n"
                  f"Is the response factually correct? JSON only.")
    for sh in bridges:
        sh_path = os.path.expandvars(sh)
        if not Path(sh_path).exists():
            continue
        try:
            req = json.dumps({"system": sys_p, "prompt": user_p,
                              "max_tokens": 200, "temperature": 0.1})
            r = subprocess.run(["bash", sh_path], input=req,
                               capture_output=True, text=True, timeout=45)
            raw = (r.stdout or "").strip()
            if not raw:
                continue
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            d = json.loads(raw)
            return {"correct": bool(d.get("correct", False)),
                    "confidence": float(d.get("confidence", 0.5)),
                    "why": d.get("why", "")[:300]}
        except Exception:
            continue
    return {"correct": False, "confidence": 0.0, "why": "judge-fail"}


def reward(prompt: str, response: str, gold: str | None = None,
           is_actually_unknown: bool | None = None) -> dict:
    """Compute TruthRL ternary reward.

    is_actually_unknown: if you have ground-truth that the answer is undefined
    (e.g., synthetic 'unanswerable' question), pass True. When unknown ↔
    abstain, reward is +1 (calibrated). Otherwise reward is 0 (model abstained
    on something it should have answered).
    """
    abstain = is_abstaining(response)

    # Path A: model abstained
    if abstain:
        if is_actually_unknown is True:
            return {"score": 1.0, "branch": "calibrated_idk",
                    "abstain": True, "correct": None, "why": "abstain on truly unknown"}
        if is_actually_unknown is False:
            return {"score": -0.3, "branch": "over_abstain",
                    "abstain": True, "correct": None,
                    "why": "abstained on a question with a real answer"}
        # No ground truth → treat abstention as neutral
        return {"score": 0.0, "branch": "abstain_neutral",
                "abstain": True, "correct": None, "why": "abstain, no oracle"}

    # Path B: model answered. Judge correctness.
    j = llm_judge_correctness(prompt, response, gold)
    if j["correct"] and j["confidence"] >= 0.6:
        return {"score": 1.0, "branch": "confident_correct",
                "abstain": False, "correct": True, "why": j["why"]}
    if not j["correct"] and j["confidence"] >= 0.6:
        return {"score": -1.0, "branch": "confident_wrong",
                "abstain": False, "correct": False, "why": j["why"]}
    # Low confidence, didn't abstain — partial credit/penalty
    return {"score": 0.2 if j["correct"] else -0.5,
            "branch": "uncertain_answer",
            "abstain": False, "correct": j["correct"], "why": j["why"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl",
                    help="batch: JSONL with {prompt, response, gold?, "
                         "is_unknown?} per line")
    ap.add_argument("--out", help="batch: output JSONL with truthrl field added")
    args = ap.parse_args()

    if args.jsonl:
        if not args.out:
            print("--out required with --jsonl", file=sys.stderr)
            sys.exit(2)
        n_in = n_out = 0
        sums = {"calibrated_idk": 0, "confident_correct": 0,
                "confident_wrong": 0, "over_abstain": 0,
                "uncertain_answer": 0, "abstain_neutral": 0}
        with open(args.jsonl) as fin, open(args.out, "w") as fout:
            for line in fin:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                n_in += 1
                d["truthrl"] = reward(
                    d.get("prompt", ""), d.get("response", ""),
                    d.get("gold"),
                    d.get("is_unknown"))
                sums[d["truthrl"]["branch"]] = sums.get(
                    d["truthrl"]["branch"], 0) + 1
                fout.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_out += 1
                if n_out % 25 == 0:
                    print(f"  graded {n_out}/{n_in}")
        print(f"[done] in={n_in} graded={n_out}")
        for k, v in sums.items():
            print(f"  {k:<22} {v:>5}")
        return

    if sys.stdin.isatty():
        print("usage: echo '{\"prompt\":...,\"response\":...}' | "
              "python3 truthrl-rewarder.py", file=sys.stderr)
        sys.exit(2)
    d = json.load(sys.stdin)
    print(json.dumps(reward(d.get("prompt", ""), d.get("response", ""),
                             d.get("gold"), d.get("is_unknown")),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
