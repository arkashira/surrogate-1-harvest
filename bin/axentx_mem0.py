"""axentx_mem0 — semantic memory layer (Mem0-inspired, runs without Docker).

Until full Mem0 is self-hosted, this gives us:
- Fact extraction from agent observations (LLM-cheap call)
- Cross-cycle memory retrieval (Postgres-backed Supabase shared_memory)
- Deduplication via fact-key hash

Usage:
    from axentx_mem0 import remember, recall
    remember("workio", "uses LINE Messaging API for clock-in", actor="codebase-indexer")
    facts = recall("workio")  # → list of facts about workio
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import urllib.parse
import urllib.request

SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))


def remember(subject: str, fact: str, actor: str = "?",
             tags: list[str] | None = None) -> bool:
    """Store a fact about a subject (project/agent/concept). Idempotent."""
    if not (SB_URL and SB_KEY and subject and fact):
        return False
    fact_hash = hashlib.md5(f"{subject}:{fact}".encode()).hexdigest()[:14]
    body = json.dumps({
        "host": os.environ.get("HOSTNAME", "?"),
        "actor": actor, "kind": "fact",
        "title": f"[{subject}] {fact[:160]}",
        "body": fact,
        "tags": ["mem0", subject] + (tags or []),
        "payload": {"subject": subject, "fact_hash": fact_hash},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_memory",
            data=body, method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def recall(subject: str, limit: int = 30) -> list[dict]:
    """Recent facts about a subject. Returns list of {fact, actor, host, ts}."""
    if not (SB_URL and SB_KEY):
        return []
    try:
        qs = urllib.parse.urlencode({
            "kind": "eq.fact",
            "title": f"like.[{subject}]*",
            "select": "title,body,actor,host,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_memory?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return []


def dedupe_recall(subject: str, limit: int = 30) -> list[str]:
    """Same as recall() but returns unique fact bodies."""
    seen = set()
    out = []
    for row in recall(subject, limit=limit * 2):
        body = (row.get("body") or "").strip()
        h = hashlib.md5(body.encode()).hexdigest()[:14]
        if h in seen:
            continue
        seen.add(h)
        out.append(body)
        if len(out) >= limit:
            break
    return out


__all__ = ["remember", "recall", "dedupe_recall"]
