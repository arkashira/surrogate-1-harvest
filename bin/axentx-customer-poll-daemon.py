#!/usr/bin/env python3
"""axentx customer-poll — pushes weekly poll questions into Supabase.

Discord bot (hermes-discord-bot) reads the queue, posts via bot client,
adds reactions, and tracks votes via on_raw_reaction_add. Two-way flow:

  poll-daemon  →  customer_polls (Supabase, status='pending')
                      ↓
              discord-bot reads
                      ↓
              posts to channel + adds ✅ ❌ 🤔 reactions
                      ↓
              status='posted'  +  posted_msg_id stored
                      ↓
              users click reactions → on_raw_reaction_add fires
                      ↓
              yes_count / no_count / maybe_count incremented in Supabase
                      ↓
              after 7 days → status='closed', written back as poll_result
                                              into the original done item

Webhook approach (commit d66fdc8) was one-way and replies were dropped.
"""
from __future__ import annotations
import datetime, json, os, sys, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import REPO_ROOT, log, call_llm, daemon_loop
POLL_SEC = int(os.environ.get("CUSTOMER_POLL_SEC", "604800"))  # 7 days

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_KEY","")

POLL_SYS = """Generate 3 yes/no/maybe poll questions to validate this product
hypothesis with real users. Each question:
- ≤140 chars, asks about USER BEHAVIOR (not opinion)
- starts with "Have you...", "Do you...", or "When did you last..."
- the YES answer should be evidence the hypothesis is real

Output strict JSON: {"questions":["q1","q2","q3"]}"""


def sb_insert_poll(item_id: str, hypothesis: str, questions: list) -> bool:
    if not (SUPABASE_URL and SUPABASE_KEY):
        log("customer-poll", "  ⚠ SUPABASE_URL/KEY missing — cannot enqueue")
        return False
    body = json.dumps({
        "item_id": item_id,
        "hypothesis": hypothesis[:500],
        "questions": questions,
        "status": "pending",
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/customer_polls",
        data=body, method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=ignore-duplicates",
            "User-Agent": "surrogate-1-customer-poll/1.0 (+server)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        # 409 conflict = duplicate item_id (already enqueued); fine
        if e.code == 409:
            return True
        log("customer-poll", f"  ✗ supabase {e.code}: {e.read()[:200].decode(errors='replace')}")
        return False
    except Exception as e:
        log("customer-poll", f"  ✗ supabase fail: {e}")
        return False


def do_one() -> bool:
    done_dir = REPO_ROOT / "state" / "swarm-shared" / "done"
    if not done_dir.exists():
        return False
    week_ago = datetime.datetime.utcnow().timestamp() - 7 * 86400
    builds = []
    for p in done_dir.glob("*.json"):
        if p.stat().st_mtime < week_ago:
            continue
        try:
            it = json.loads(p.read_text())
            biz = it.get("business_verdict", {}) or {}
            if (biz.get("verdict") or "").upper() == "BUILD":
                builds.append(it)
        except Exception:
            continue
    if not builds:
        log("customer-poll", "no BUILD opportunities this week")
        return False
    builds.sort(key=lambda i: -(i.get("verdict", {}).get("severity", 0)))
    item = builds[0]
    bd = item.get("bd_verdict", {}) or {}
    hypothesis = bd.get("feature_one_liner") or bd.get("new_product_one_liner", "?")
    audience = item.get("verdict", {}).get("audience", "")

    try:
        out = call_llm(
            f"Hypothesis: {hypothesis}\nAudience: {audience}",
            system=POLL_SYS, max_tokens=300, timeout=30,
        )
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
        d = json.loads(txt.strip())
    except Exception as e:
        log("customer-poll", f"llm fail: {e}")
        return False

    questions = d.get("questions", [])[:3]
    if len(questions) < 3:
        return False

    ok = sb_insert_poll(item["id"], hypothesis, questions)
    if ok:
        log("customer-poll", f"✓ enqueued for bot to post: {item['id'][:30]}  ({len(questions)} q's)")
    return ok


if __name__ == "__main__":
    daemon_loop("customer-poll", POLL_SEC, do_one)
