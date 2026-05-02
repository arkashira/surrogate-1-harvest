#!/usr/bin/env python3
"""axentx pain validator — quality gate between research and BD.

User directive (2026-05-02):
  > "วิเคราะห์ว่า ปัญหานั้นมันคืออะไร แล้วปัญหานั้น คนอื่นก็มีปัญหาด้วยไหม
  >  หรือแค่คนๆเดียวเป็น แล้ว validate ว่า มันคือปัญหานั้น เกิดจากอะไร
  >  เพราะอะไร แล้วค่อยๆ เอาสิ่งที่ทำให้เกิดปัญหา พวกนั้น มาทำ design thinking"

Sits between research-queue and bd-queue:
  research → bd-queue (item sits here briefly)
  validator picks up bd-queue items
  for each, asks: "Is this a real recurring pain, or one person's bad day?"
  Validates by:
    1. RAG search across our own corpus — has the same pain shown up before?
    2. Cross-source confirmation — search GitHub Issues / Stack Exchange
       for the same symptom; require ≥ N neighbors with similar pain.
    3. Severity recalibration based on reach (audience size signal).
  Output:
    - confirmed=True  → enriches the item (validator_verdict.confidence,
                        neighbors_cited[]) and re-emits to bd-queue
    - confirmed=False → moves to done/ with reason="not-validated" so we
                        save BD/design/business/marketing cycles
                        downstream.

Why this exists:
  Without validation, every reddit-rant gets a full BD→design→business→
  marketing→PRD→dev pipeline run. That burns LLM tokens on noise. With
  validation gate, only validated pains advance — concentrating energy
  on real opportunities.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm_strong, pick_oldest, advance,
                             fail, daemon_loop, rag_query, write_item)

POLL_SEC = int(os.environ.get("VALIDATOR_POLL_SEC", "60"))
MIN_NEIGHBORS = int(os.environ.get("VALIDATOR_MIN_NEIGHBORS", "2"))
GH_TOKEN = (os.environ.get("AXENTX_BOT_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN", ""))
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


VALIDATE_SYSTEM = """You are a market-validation analyst. Given:
  1. A pain point extracted from a single post
  2. Top-K neighbors from our own RAG corpus (similar pains seen before)
  3. K external search hits from GitHub Issues + Stack Exchange

Decide whether this is a REAL recurring pain or noise.

Output STRICT JSON:
{
  "confirmed": true|false,
  "confidence": 0.0-1.0,
  "audience_size_estimate": "single-person|small-niche|large-niche|broad",
  "root_cause": "<root cause in 1-2 sentences, grounded in evidence>",
  "neighbors_cited": ["<URL or source-id>", "..."],
  "rationale": "<2-3 sentences why confirmed or rejected>"
}

CONFIRM only if:
  - ≥ 2 distinct neighbors share a meaningfully similar pain (not just
    related topic)
  - The root cause can be articulated with evidence (don't speculate)
  - audience_size_estimate ≥ small-niche

REJECT (confirmed=false) if:
  - One-off complaint, no neighbors with same pain
  - Pain is too vague to articulate root cause
  - Already-solved-elsewhere (good answers exist for this exact problem)
"""


def gh_search_issues(query: str, n: int = 5) -> list[dict]:
    headers = {"User-Agent": UA_BROWSER,
               "Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    url = (f"https://api.github.com/search/issues"
           f"?q={urllib.parse.quote(query)}+is:issue&sort=reactions&per_page={n}")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return [
            {"source": "gh-issues", "url": it.get("html_url", ""),
             "title": (it.get("title") or "")[:200],
             "snippet": (it.get("body") or "")[:300],
             "score": (it.get("reactions") or {}).get("total_count", 0)}
            for it in (d.get("items") or [])[:n]
        ]
    except Exception:
        return []


def se_search(query: str, n: int = 5) -> list[dict]:
    """Stack Exchange site=stackoverflow advanced search by tag-or-keyword."""
    url = (f"https://api.stackexchange.com/2.3/search/advanced"
           f"?order=desc&sort=relevance&site=stackoverflow"
           f"&q={urllib.parse.quote(query)}&pagesize={n}")
    try:
        import gzip
        req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            d = json.loads(raw)
        return [
            {"source": "stackoverflow",
             "url": it.get("link", ""),
             "title": (it.get("title") or "")[:200],
             "snippet": "",
             "score": it.get("score", 0)}
            for it in (d.get("items") or [])[:n]
        ]
    except Exception:
        return []


def gather_neighbors(pain_text: str) -> list[dict]:
    """RAG (own corpus) + GitHub Issues + Stack Overflow."""
    neighbors: list[dict] = []
    # 1. RAG over our own decisions/papers/skills
    try:
        rag_block = rag_query(pain_text, top_k=5, kind="pain")
        if rag_block:
            for line in rag_block.splitlines():
                if line.strip().startswith("- "):
                    neighbors.append({
                        "source": "rag", "url": "internal",
                        "title": line[2:][:200], "snippet": "", "score": 0,
                    })
    except Exception:
        pass
    # 2. GitHub issues
    short_q = pain_text[:120]
    neighbors.extend(gh_search_issues(short_q, n=4))
    # 3. Stack Overflow
    neighbors.extend(se_search(short_q, n=4))
    return neighbors[:12]


def do_one_validation() -> bool:
    picked = pick_oldest("validator")
    if not picked:
        return False
    src_path, item = picked
    pain = item.get("verdict", {}) or {}
    pain_text = pain.get("pain_one_liner") or ""
    if not pain_text:
        # No pain text to validate — pass-through to BD as-is.
        item["validator_verdict"] = {
            "confirmed": True, "confidence": 0.5,
            "rationale": "no pain_one_liner — pass-through",
        }
        advance(item, src_path, "bd", "validator", "PASS-THROUGH (no pain_one_liner)")
        return True

    log("validator", f"▸ {item['id'][:30]}  '{pain_text[:60]}'")
    neighbors = gather_neighbors(pain_text)
    nbr_block = "\n".join(
        f"  [{n.get('source')}] {n.get('title','')[:120]}  ({n.get('url','')})"
        for n in neighbors
    ) or "  (no neighbors found)"

    user = (
        f"Pain: {pain_text}\n"
        f"Audience: {pain.get('audience','?')}\n"
        f"Severity (extractor): {pain.get('severity','?')}\n"
        f"Evidence quote: {pain.get('evidence','')[:300]}\n\n"
        f"Neighbors found ({len(neighbors)}):\n{nbr_block}\n\n"
        f"Output strict JSON validation verdict per schema."
    )
    try:
        # Validator is permissive — if every strong provider fails (rate-
        # limit storm), we'd rather get a mid-tier verdict than no verdict.
        # BD itself runs strict (allow_degrade=False).
        out = call_llm_strong(user, system=VALIDATE_SYSTEM,
                              max_tokens=900, timeout=45,
                              allow_degrade=True)
    except Exception as e:
        log("validator", f"  ⚠ strong-llm failed: {e}; passing through to BD")
        item["validator_verdict"] = {
            "confirmed": True, "confidence": 0.3,
            "rationale": f"validator-fault: {str(e)[:80]}",
        }
        advance(item, src_path, "bd", "validator",
                f"FALLTHROUGH (llm-fault: {str(e)[:80]})")
        return True
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```")[1]
        if seg.startswith("json"):
            seg = seg[4:]
        txt = seg.strip()
    try:
        verdict = json.loads(txt)
    except Exception as e:
        log("validator", f"  ⚠ JSON parse fail; passing through: {e}")
        item["validator_verdict"] = {
            "confirmed": True, "confidence": 0.3,
            "rationale": f"parse-fault: {str(e)[:80]}",
        }
        advance(item, src_path, "bd", "validator",
                f"FALLTHROUGH (parse-fault: {str(e)[:80]})")
        return True

    item["validator_verdict"] = verdict
    item.setdefault("history", []).append({
        "stage": "validator",
        "actor": "axentx-pain-validator",
        "output": json.dumps(verdict, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })

    n_neighbors = len(verdict.get("neighbors_cited") or [])
    if not verdict.get("confirmed") or n_neighbors < MIN_NEIGHBORS:
        # Park in done/ with rejection reason
        item["current"] = item.get("current") or {}
        item["current"]["text"] = json.dumps(verdict, ensure_ascii=False)
        advance(item, src_path, "done", "validator",
                f"REJECTED: confirmed={verdict.get('confirmed')} "
                f"neighbors={n_neighbors} — "
                f"{(verdict.get('rationale','') or '')[:120]}")
        log("validator",
            f"  ✗ rejected (conf={verdict.get('confidence',0):.2f}, "
            f"audience={verdict.get('audience_size_estimate','?')})")
        return True

    # Validated — re-emit to bd-queue with enriched verdict.
    item["current"] = item.get("current") or {}
    item["current"]["text"] = json.dumps(verdict, ensure_ascii=False)
    advance(item, src_path, "bd", "validator",
            f"VALIDATED conf={verdict.get('confidence',0):.2f} "
            f"audience={verdict.get('audience_size_estimate','?')} "
            f"neighbors={n_neighbors}")
    log("validator",
        f"  ✓ validated (conf={verdict.get('confidence',0):.2f}, "
        f"audience={verdict.get('audience_size_estimate','?')})")
    return True


if __name__ == "__main__":
    daemon_loop("validator", POLL_SEC, do_one_validation)
