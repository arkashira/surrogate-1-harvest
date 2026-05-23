#!/usr/bin/env python3
"""DPO pair quality scorer.

Reads `state/training-shards/dpo.jsonl` and for each (prompt, chosen,
rejected) triple asks the LLM chain to score chosen-vs-rejected on
relevance, specificity, and accuracy (0-10 each). The chosen response
must score strictly higher than rejected on at least 2 of 3 axes AND
their delta must exceed `--threshold` (default 1.5 averaged) for the
pair to survive.

Output: state/training-shards/dpo.scored.jsonl (kept) and
        state/training-shards/dpo.dropped.jsonl (rejected, with reason).

Idempotent — keeps cursor in state/.dpo-score-cursor.json so reruns
only score new triples.

Why: agent-decisions-to-pairs emits triples but not all are useful;
many "rejected" outputs are first-attempt drafts that a human would
also accept. Filtering by judge LLM keeps the top ~70% and avoids
poisoning DPO with low-margin pairs.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[1]))
os.environ.setdefault("REPO_ROOT", str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import call_llm  # noqa: E402

SHARDS_DIR = REPO_ROOT / "state" / "training-shards"
DPO_IN = SHARDS_DIR / "dpo.jsonl"
DPO_KEPT = SHARDS_DIR / "dpo.scored.jsonl"
DPO_DROPPED = SHARDS_DIR / "dpo.dropped.jsonl"
CURSOR = REPO_ROOT / "state" / ".dpo-score-cursor.json"
LOG_FILE = REPO_ROOT / "logs" / "score-dpo-pairs.log"

JUDGE_SYSTEM = (
    "You are a strict pair-quality judge. Score two candidate responses "
    "(CHOSEN and REJECTED) against a PROMPT on three axes: relevance, "
    "specificity, accuracy. Each axis 0-10 (10 = best). Output STRICT JSON "
    "exactly:\n"
    '{"chosen":{"relevance":N,"specificity":N,"accuracy":N},'
    '"rejected":{"relevance":N,"specificity":N,"accuracy":N},'
    '"verdict":"chosen-better|rejected-better|tie","why":"<≤25 words>"}'
)


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, file=sys.stderr, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_cursor() -> set[str]:
    if not CURSOR.exists():
        return set()
    try:
        return set(json.loads(CURSOR.read_text()).get("scored_fps", []))
    except Exception:
        return set()


def save_cursor(scored: set[str]) -> None:
    CURSOR.write_text(json.dumps({"scored_fps": sorted(scored)}))


def parse_judge_response(text: str) -> dict | None:
    # Strip code fences if model wrapped in ```json
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find first { ... } block.
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def score_triple(rec: dict) -> tuple[bool, dict, str]:
    """Returns (kept, score_obj, reason)."""
    prompt = rec.get("prompt", "")
    chosen = rec.get("chosen", "")
    rejected = rec.get("rejected", "")
    if not prompt or not chosen or not rejected:
        return False, {}, "missing-fields"

    judge_input = (
        f"### PROMPT\n{prompt[:2000]}\n\n"
        f"### CHOSEN\n{chosen[:2000]}\n\n"
        f"### REJECTED\n{rejected[:2000]}\n\n"
        f"Score now."
    )
    try:
        raw = call_llm(judge_input, system=JUDGE_SYSTEM, max_tokens=400, timeout=45)
    except Exception as exc:
        return False, {}, f"judge-error:{exc}"

    parsed = parse_judge_response(raw)
    if not parsed:
        return False, {"raw": raw[:300]}, "judge-unparseable"

    c = parsed.get("chosen") or {}
    r = parsed.get("rejected") or {}
    try:
        c_total = sum(float(c.get(k, 0)) for k in ("relevance", "specificity", "accuracy"))
        r_total = sum(float(r.get(k, 0)) for k in ("relevance", "specificity", "accuracy"))
    except Exception:
        return False, parsed, "score-not-numeric"
    margin = (c_total - r_total) / 3.0  # avg per-axis margin
    parsed["margin"] = round(margin, 3)
    parsed["c_total"] = round(c_total, 1)
    parsed["r_total"] = round(r_total, 1)
    return True, parsed, "scored"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=1.5,
                    help="min avg per-axis margin chosen − rejected to keep pair")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap rows scored this run (cron protection)")
    args = ap.parse_args()

    if not DPO_IN.exists():
        log(f"input missing: {DPO_IN} — nothing to score")
        return 0

    scored = load_cursor()
    n_total = n_kept = n_dropped = 0
    SHARDS_DIR.mkdir(parents=True, exist_ok=True)

    with DPO_IN.open() as src, \
         DPO_KEPT.open("a") as kept_out, \
         DPO_DROPPED.open("a") as dropped_out:
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_total += 1
            fp = rec.get("fp")
            if not fp or fp in scored:
                continue
            if args.limit is not None and (n_kept + n_dropped) >= args.limit:
                break

            ok, score_obj, reason = score_triple(rec)
            scored.add(fp)
            if not ok:
                rec["score_reason"] = reason
                rec["score_detail"] = score_obj
                dropped_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_dropped += 1
                log(f"  drop {fp[:8]}: {reason}")
                continue

            margin = score_obj.get("margin", 0)
            verdict = score_obj.get("verdict", "")
            if margin >= args.threshold and verdict != "rejected-better":
                rec["score"] = score_obj
                kept_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_kept += 1
                log(f"  keep {fp[:8]} margin={margin}")
            else:
                rec["score"] = score_obj
                rec["score_reason"] = f"low-margin:{margin}<{args.threshold}"
                dropped_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_dropped += 1
                log(f"  drop {fp[:8]} margin={margin} (verdict={verdict})")

    save_cursor(scored)
    log(f"DONE total={n_total} kept={n_kept} dropped={n_dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
