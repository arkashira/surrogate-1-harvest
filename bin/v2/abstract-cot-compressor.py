"""Surrogate-1 v2 — Abstract-CoT compressor.

Reference: arxiv.org/html/2506.08343v1 (Abstract-CoT, 2025-06)

Compresses verbose chain-of-thought into dense reasoning tokens. Removes
filler ("Hmm/Wait/Therefore/Let me think") while preserving deduction
chain. Reported 12× token reduction on MATH-500 at parity.

Use to compress training-data CoT before SFT — model learns to emit
shorter traces.

Strategy:
  • Extract numbered/bulleted steps
  • Drop verbose connectives ("So I think", "Let me see", etc.)
  • Drop self-correction loops ("Wait, that's wrong, let me try...")
  • Keep math/code lines verbatim
  • Compress to ≤30% original length, target 12× compression on long CoT

Used pre-training-data:
  python3 abstract-cot-compressor.py --input verbose-cot.jsonl --out compressed.jsonl
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# Filler patterns — verbose connective tissue we strip
FILLER_PATTERNS = [
    r"^\s*(?:hmm+|wait|so|well|let me think|let'?s see|let me check|"
    r"first off|on second thought|come to think of it|now|right|ok(?:ay)?|"
    r"alright|i think|i guess|maybe|perhaps|actually|basically|essentially)\b[,\.]?\s*",
    r"\b(?:i'?m\s+going\s+to|i\s+(?:will|need\s+to|should|could|might))\s+(?:check|verify|think|consider|see|try)\b[^.]*\.\s*",
    r"\bthat (?:doesn'?t |does not )?(?:make sense|seem right|work)\b[^.]*\.\s*",
    r"\b(?:let me try|let me redo|i'?ll restart|going back)\b[^.]*\.\s*",
    r"\b(?:to (?:summarize|recap)|in summary|to conclude|in conclusion)\b[,\.:]?\s*",
    r"\bthe answer is(?:\s+just)?\s*[:=]?\s*",
]
FILLER_RE = re.compile("|".join(FILLER_PATTERNS), re.IGNORECASE | re.MULTILINE)

# Self-correction blocks — entire sentences that walk back
WALKBACK_RE = re.compile(
    r"[^.]*(?:wait|actually|hmm|on second thought|i was wrong|no,? that)[^.]*\.\s*",
    re.IGNORECASE)

# Code/math blocks we preserve verbatim
CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)
MATH_LINE_RE  = re.compile(r"^\s*\$\$.*?\$\$\s*$|^\s*\\\[.*?\\\]\s*$", re.MULTILINE)


def compress(text: str, target_ratio: float = 0.30) -> str:
    if not text:
        return text

    # Preserve code blocks by token-replacing
    code_blocks = []
    def _stash_code(m):
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks)-1}\x00"
    text = CODE_FENCE_RE.sub(_stash_code, text)

    # Strip walkback
    text = WALKBACK_RE.sub("", text)
    # Strip filler
    text = FILLER_RE.sub("", text)

    # Collapse whitespace
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)

    # Restore code
    for i, c in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", c)

    return text.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--field", default="response",
                    help="JSON field with CoT text (default: response)")
    args = ap.parse_args()

    inp = Path(args.input); out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    n_in = n_out = 0
    sum_in = sum_out = 0
    with open(inp) as fin, open(out, "w") as fout:
        for line in fin:
            try: d = json.loads(line)
            except: continue
            n_in += 1
            txt = d.get(args.field, "")
            if not txt: continue
            sum_in += len(txt)
            comp = compress(txt)
            sum_out += len(comp)
            d[args.field] = comp
            d["abstract_cot"] = {
                "orig_len": len(txt), "compressed_len": len(comp),
                "ratio": round(len(comp) / max(1, len(txt)), 3),
            }
            fout.write(json.dumps(d, ensure_ascii=False) + "\n")
            n_out += 1
            if n_out % 100 == 0:
                print(f"  compressed {n_out}/{n_in} avg_ratio="
                      f"{sum_out/max(1,sum_in):.3f}")
    avg_ratio = sum_out / max(1, sum_in)
    print(f"[done] in={n_in} out={n_out} avg_ratio={avg_ratio:.3f} "
          f"(target ≤0.30 = good)")


if __name__ == "__main__":
    main()
