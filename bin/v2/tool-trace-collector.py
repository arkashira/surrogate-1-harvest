"""Surrogate-1 v2 — Tool-trace collector.

Mines vLLM/orchestrate tool-call logs and Hermes XML traces, curates
into:
  • SFT (successful trajectories) → ~/.surrogate/data/v2/tool-traces-sft.jsonl
  • DPO (success vs failed retry pairs) → ~/.surrogate/data/v2/tool-traces-dpo.jsonl

Detects:
  Hermes XML format: <tool_call>{"name":..., "arguments":...}</tool_call>
                      <tool_response>...</tool_response>
  ChatML JSON-args format from OpenAI compat
  Failed calls: tool_response containing 'error|exception|traceback|HTTP 4|HTTP 5'

Skill candidates: extract (tool_name, args_schema, success_args) tuples;
hand to voyager-skills.py for promotion.

Run: python3 tool-trace-collector.py [--since 2026-04-01]
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from sanitize import filter_pair  # type: ignore
except Exception:
    def filter_pair(p, r):
        return {"keep": True}

LOG_DIRS = [
    Path.home() / ".surrogate/logs",
    Path.home() / ".surrogate/state/orchestrate",
    Path("/data/logs"),
    Path("/data/state/orchestrate"),
]
OUT_SFT = Path.home() / ".surrogate/data/v2/tool-traces-sft.jsonl"
OUT_DPO = Path.home() / ".surrogate/data/v2/tool-traces-dpo.jsonl"
HERMES_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>\s*"
    r"(?:<tool_response>\s*(.*?)\s*</tool_response>)?",
    re.DOTALL)
ERROR_HINTS = re.compile(
    r"\b(?:error|exception|traceback|stderr|"
    r"HTTP\s*[45]\d\d|status[\s_]*code[\s:=]*[45]\d\d|"
    r"failed|denied|unauthorized|forbidden|not\s+found)\b",
    re.IGNORECASE)


def _load_voyager():
    try:
        spec = importlib.util.spec_from_file_location(
            "voyager_skills",
            str(Path.home() / ".surrogate/bin/v2/voyager-skills.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod
    except Exception:
        return None


def _is_failure(resp: str) -> bool:
    if not resp:
        return False
    if len(resp) < 10:
        return True
    return bool(ERROR_HINTS.search(resp[:2000]))


def _iter_logs(since_ts: int) -> Iterator[Path]:
    for d in LOG_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.log"):
            try:
                if p.stat().st_mtime >= since_ts and p.stat().st_size > 0:
                    yield p
            except OSError:
                continue
        for p in d.rglob("*.jsonl"):
            try:
                if p.stat().st_mtime >= since_ts and p.stat().st_size > 0:
                    yield p
            except OSError:
                continue


def _extract_traces(text: str) -> list[dict]:
    """Pull (tool, args, response, success) tuples from a log blob."""
    out = []
    for m in HERMES_RE.finditer(text):
        try:
            call = json.loads(m.group(1))
            name = call.get("name") or call.get("tool") or ""
            args = call.get("arguments") or call.get("args") or {}
            resp = (m.group(2) or "").strip()
            if not name:
                continue
            out.append({
                "tool": name,
                "args": args,
                "response": resp[:3000],
                "success": not _is_failure(resp),
            })
        except json.JSONDecodeError:
            continue
    return out


def _trace_to_pair(prompt_ctx: str, traces: list[dict]) -> dict | None:
    if not traces:
        return None
    msgs = []
    for t in traces:
        msgs.append({
            "role": "assistant",
            "tool_call": {"name": t["tool"], "arguments": t["args"]},
        })
        msgs.append({"role": "tool", "content": t["response"]})
    asst_text = "\n".join(
        f"<tool_call>{json.dumps({'name': t['tool'], 'arguments': t['args']})}</tool_call>\n"
        f"<tool_response>{t['response'][:1000]}</tool_response>"
        for t in traces)
    if not filter_pair(prompt_ctx, asst_text)["keep"]:
        return None
    return {
        "prompt": prompt_ctx[:4000],
        "response": asst_text[:200000],
        "source": "tool-trace",
        "meta": {
            "n_calls": len(traces),
            "n_failed": sum(1 for t in traces if not t["success"]),
            "tools": list({t["tool"] for t in traces}),
        },
    }


def _split_success_fail(traces: list[dict]) -> tuple[list[dict], list[dict]]:
    return ([t for t in traces if t["success"]],
            [t for t in traces if not t["success"]])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO date, default: last 24h")
    ap.add_argument("--max", type=int, default=5000)
    args = ap.parse_args()

    if args.since:
        from datetime import datetime
        since_ts = int(datetime.fromisoformat(args.since).timestamp())
    else:
        since_ts = int(time.time()) - 24 * 3600

    OUT_SFT.parent.mkdir(parents=True, exist_ok=True)
    voyager = _load_voyager()
    seen: set[str] = set()
    n_sft = 0
    n_dpo = 0

    with open(OUT_SFT, "a") as fs, open(OUT_DPO, "a") as fd:
        for log in _iter_logs(since_ts):
            try:
                text = log.read_text(errors="ignore")[:2_000_000]
            except OSError:
                continue
            traces = _extract_traces(text)
            if not traces:
                continue
            # rough prompt context = first 1500 chars before first tool_call
            first_call = HERMES_RE.search(text)
            prompt_ctx = text[:first_call.start() if first_call else 0]
            prompt_ctx = prompt_ctx[-2000:].strip() or "(no prompt context found)"
            sig = hashlib.md5(
                (str(log) + prompt_ctx[:200] + str(len(traces)))
                .encode()).hexdigest()[:16]
            if sig in seen:
                continue
            seen.add(sig)

            wins, fails = _split_success_fail(traces)

            # SFT from successful trajectories
            sft = _trace_to_pair(prompt_ctx, wins)
            if sft:
                fs.write(json.dumps(sft, ensure_ascii=False) + "\n")
                n_sft += 1

            # DPO when both win + fail attempts present (retry pattern)
            if wins and fails:
                pair = {
                    "prompt": prompt_ctx[:4000],
                    "chosen": "\n".join(
                        f"<tool_call>{json.dumps({'name': t['tool'], 'arguments': t['args']})}</tool_call>"
                        for t in wins),
                    "rejected": "\n".join(
                        f"<tool_call>{json.dumps({'name': t['tool'], 'arguments': t['args']})}</tool_call>"
                        for t in fails),
                    "source": "tool-trace-dpo",
                }
                fd.write(json.dumps(pair, ensure_ascii=False) + "\n")
                n_dpo += 1

            # Voyager skills: each successful tool call becomes a skill candidate
            if voyager:
                for t in wins:
                    name = f"tool_{t['tool']}_{hashlib.md5(json.dumps(t['args'], sort_keys=True).encode()).hexdigest()[:8]}"
                    code = json.dumps(
                        {"name": t["tool"], "arguments": t["args"]},
                        ensure_ascii=False, indent=2)
                    voyager.add(name, code,
                                description=f"Tool call to {t['tool']}",
                                tags=[t["tool"], "tool-call"])
                    voyager.record(name, success=True)

            if n_sft + n_dpo >= args.max:
                break

    print(f"[done] sft={n_sft} dpo={n_dpo} since={since_ts}")


if __name__ == "__main__":
    main()
