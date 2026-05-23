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





# 2026-05-11 fast-path heuristic (skip LLM for clear-signal items)
# Heuristic fast-path: most items have clear signals that don't need LLM.
# Score by source + monetary tags + content patterns. Returns:
#   ("pass", reason)  → skip LLM, advance to next stage
#   ("kill", reason)  → spam/trash, advance to done
#   ("llm",  reason)  → fall through to LLM gather_neighbors path

import re as _re_fp

# Spam / trash patterns — pure noise
_KILL_PATTERNS = [
    "upvote please", "please upvote", "subscribe to my", "follow me on",
    "check my channel", "check my profile", "promote my", "i made a video",
    "buy followers", "free crypto", "click here to", "limited time offer",
]

# Strong-monetary intent — go through
_MONETARY_KEYWORDS = [
    "willing to pay", "i would pay", "would happily pay",
    "looking to hire", "looking for a tool that",
    "anyone built", "is there a tool", "is there an app",
    "alternative to", "saas for", "tool for",
    "$ /mo", "$ /month", "$ per month",
]

# Decision-maker title (founder, CTO, VP — paying buyer)
_DECISION_TITLES = [
    "founder", "co-founder", "cofounder", "cto", "vp of", "head of",
    "director of", "vp engineering", "engineering manager",
]


def _heuristic_validate(item: dict) -> tuple[str, str]:
    """Return (decision, reason). decision in {"pass","kill","llm"}."""
    post = item.get("post") or {}
    pain = item.get("verdict") or {}
    pain_text = (pain.get("pain_one_liner") or "")[:300]
    title = (post.get("title") or "")[:300]
    body = (post.get("body") or "")[:1500]
    source = (post.get("source") or "").lower()
    sig = (item.get("monetary_signal") or "").lower()
    score = item.get("monetary_intent_score") or 0
    text = (title + " " + body + " " + pain_text).lower()

    # 1. TRACK C trend items — handled by trend-arb, NOT pain-validator
    if item.get("track") == "C" or "trend:" in source:
        return ("kill", "track-C item — handled by trend-arb")

    # 2. Validated-source items — already validated, pass straight
    if (item.get("validated_source") or
        source.startswith("validated:")):
        return ("pass", "validated-source — straight to bd")

    # 3. High monetary signal — pass
    if sig == "high" and score and score >= 7:
        return ("pass", f"high-money sig (score={score})")

    # 4. Funded/job sources — implicit-money, pass
    if (source.startswith("fund:") or source.startswith("jobs:") or
        "remoteok" in source or "wellfound" in source or
        "algora" in source):
        return ("pass", "funded/job source — implicit money")

    # 5. Strong monetary keywords in title
    if any(k in title.lower() for k in _MONETARY_KEYWORDS):
        return ("pass", "monetary phrase in title")

    # 6. Decision-maker self-identification
    if any(_re_fp.search(rf"\b{t}\b", text) for t in _DECISION_TITLES):
        return ("pass", "decision-maker title")

    # 7. Spam / trash kill — fast
    if any(p in text for p in _KILL_PATTERNS):
        return ("kill", "spam pattern")

    # 8. Title too short — likely low-quality
    if len(title) < 20:
        return ("kill", f"title too short ({len(title)} chars)")

    # 9. No pain text + no useful body — pass-through (bd will decide)
    if not pain_text and len(body) < 100:
        return ("pass", "no pain text + thin body — pass-through")

    # 10. Default: needs LLM
    return ("llm", "needs LLM analysis")


def do_one_validation() -> bool:
    picked = pick_oldest("validator")
    if not picked:
        return False
    src_path, item = picked

    # ── HEURISTIC FAST-PATH (skip LLM for ~70% of items) ──
    h_decision, h_reason = _heuristic_validate(item)
    if h_decision == "pass":
        log("validator", f"  ✓P[heur] {item['id'][:30]} ({h_reason})")
        item["validator_verdict"] = {
            "confirmed": True, "confidence": 0.7,
            "rationale": f"[heuristic-pass] {h_reason}",
            "_heuristic": True,
        }
        advance(item, src_path, "market-research", "validator",
                f"PASS-THROUGH[heuristic] {h_reason}")
        return True
    if h_decision == "kill":
        log("validator", f"  ⊘K[heur] {item['id'][:30]} ({h_reason})")
        item["validator_verdict"] = {
            "confirmed": False, "confidence": 0.9,
            "rationale": f"[heuristic-kill] {h_reason}",
            "_heuristic": True,
        }
        advance(item, src_path, "done", "validator",
                f"KILL[heuristic] {h_reason}")
        return True
    # h_decision == "llm" — fall through

    pain = item.get("verdict", {}) or {}
    pain_text = pain.get("pain_one_liner") or ""
    if not pain_text:
        item["validator_verdict"] = {
            "confirmed": True, "confidence": 0.5,
            "rationale": "no pain_one_liner — pass-through",
        }
        advance(item, src_path, "market-research", "validator", "PASS-THROUGH (no pain_one_liner)")
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
        # Route through market-research first so bd has TAM/SAM/SOM data
        # to make a numerical NEW-PRODUCT vs EXTEND vs PASS call.
        advance(item, src_path, "market-research", "validator",
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
        # Route through market-research first so bd has TAM/SAM/SOM data
        # to make a numerical NEW-PRODUCT vs EXTEND vs PASS call.
        advance(item, src_path, "market-research", "validator",
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
