#!/usr/bin/env python3
"""axentx docs — auto-updates README + CHANGELOG when public surface changes.

Polls swarm-shared/done/ for items with stage='commit' that touched files
under bin/ or src/. Asks LLM to draft README diff + CHANGELOG entry.
Writes new docs commit through commit-queue (re-uses commit daemon's push)."""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, new_item, write_item)
POLL_SEC = int(os.environ.get("DOCS_POLL_SEC", "300"))

DOCS_SYSTEM = """You are a technical writer. For each shipped commit, propose
short doc updates. Output strict JSON:

{
  "needs_update": true|false,
  "readme_addition": "<markdown to append, or empty>",
  "changelog_entry": "<one bullet for CHANGELOG.md, or empty>"
}

Set needs_update=false if the commit is internal-only (test, refactor,
chore). Set true only if a user-facing capability changed."""

CURSOR_FILE = REPO_ROOT / "state" / ".docs-daemon-cursor.json"

def load_seen():
    try: return set(json.loads(CURSOR_FILE.read_text()).get("seen",[]))
    except: return set()
def save_seen(seen):
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps({"seen": sorted(seen)[-2000:]}))

def do_one() -> bool:
    seen = load_seen()
    done_dir = REPO_ROOT / "state" / "swarm-shared" / "done"
    if not done_dir.exists(): return False
    candidates = sorted(done_dir.glob("*.json"), key=lambda p: -p.stat().st_mtime)[:10]
    for path in candidates:
        if path.name in seen: continue
        try: it = json.loads(path.read_text())
        except: continue
        # only docs-pass items that came from commit
        if not any(h.get("stage") == "commit" for h in it.get("history", [])): continue
        seen.add(path.name)
        save_seen(seen)
        proposal = (it.get("current",{}) or {}).get("text","")[:2500]
        try:
            out = call_llm(
                f"Project: {it.get('project','?')}\nCommit summary:\n{proposal}\n\nDraft doc update.",
                system=DOCS_SYSTEM, max_tokens=600, timeout=30,
            )
            txt = out.strip()
            if "```" in txt: txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
            d = json.loads(txt.strip())
        except Exception as e:
            log("docs", f"⚠ parse fail: {e}"); continue
        if not d.get("needs_update"): continue
        # push a new dev item that the commit daemon will package + push
        ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        doc_item = new_item(it.get("project","surrogate-1"), "docs",
                            f"docs auto-update: {it['id'][:20]}")
        doc_item["history"] = [{"stage":"dev","actor":"axentx-docs",
                                "output": (
                                    f"Auto-doc update for commit {it['id']}.\n\n"
                                    f"README addition:\n{d.get('readme_addition','')}\n\n"
                                    f"CHANGELOG entry:\n- {d.get('changelog_entry','')}\n"
                                ),
                                "at": datetime.datetime.utcnow().isoformat()+"Z"}]
        doc_item["current"]["text"] = doc_item["history"][0]["output"]
        write_item(doc_item, "commit")  # straight to commit (no review needed for docs)
        log("docs", f"  ✓ doc update queued for {it['id'][:24]}")
        return True
    return False

if __name__ == "__main__":
    daemon_loop("docs", POLL_SEC, do_one)
