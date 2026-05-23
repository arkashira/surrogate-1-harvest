#!/usr/bin/env python3
"""axentx knowledge-ingest daemon — siphons shared_* tables into the
HuggingFace training dataset axentx/shared-context-stream.

Closes the self-improvement loop:

    Mac/Hosts write to shared_kv / shared_memory / shared_knowledge (live)
                            │
                            ▼
       (this daemon, every N minutes)
                            │
                            ▼
   Snapshot → JSONL chunk → push to HF dataset
                            │
                            ▼
      training-pairs harvester picks up + LoRA fine-tune
                            │
                            ▼
           next-gen surrogate model has all 3-host wisdom

Idempotent: tracks `last_seen_id` per table in cost-guard state dir.
Each cycle pushes only new rows since the last cursor.

Output format on HF: rows of {kind, slug, title, body, host, actor,
created_at, tags, payload}. Compatible with the existing axentx training
loader (which expects flat row schema).
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

POLL_SEC = int(os.environ.get("INGEST_POLL_SEC", "600"))   # every 10min
HF_DATASET = os.environ.get("HF_INGEST_DATASET",
                            "axentx/shared-context-stream")
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
HF_TOKEN = os.environ.get("HF_TOKEN", "")

STATE_FILE = REPO_ROOT / "state" / "knowledge-ingest.state.json"
STAGE_DIR = REPO_ROOT / "state" / "knowledge-ingest"
STAGE_DIR.mkdir(parents=True, exist_ok=True)

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _sb_get(path: str, params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{SB_URL}{path}?{qs}",
        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log("ingest", f"  ✗ SB fetch {path}: {type(e).__name__}: {str(e)[:120]}")
        return []


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2))


def fetch_new_rows(state: dict) -> list[dict]:
    """Pull rows newer than the last seen cursor from each shared table."""
    rows: list[dict] = []
    # shared_memory uses bigserial id — easy cursor
    last_mem = state.get("last_memory_id", 0)
    new_mem = _sb_get("/rest/v1/shared_memory", {
        "select": "*",
        "id": f"gt.{last_mem}",
        "order": "id.asc",
        "limit": "1000",
    })
    for r in new_mem:
        rows.append({
            "kind": "memory",
            "row_id": r["id"],
            "slug": f"memory/{r['id']}",
            "title": r.get("title", ""),
            "body": r.get("body", ""),
            "host": r.get("host"),
            "actor": r.get("actor"),
            "created_at": r.get("created_at"),
            "tags": r.get("tags") or [],
            "payload": r.get("payload"),
        })
    if new_mem:
        state["last_memory_id"] = max(r["id"] for r in new_mem)

    # shared_knowledge uses updated_at (slug is stable, content can update)
    last_kn = state.get("last_knowledge_at", "1970-01-01T00:00:00Z")
    new_kn = _sb_get("/rest/v1/shared_knowledge", {
        "select": "*",
        "updated_at": f"gt.{last_kn}",
        "order": "updated_at.asc",
        "limit": "1000",
    })
    for r in new_kn:
        rows.append({
            "kind": "knowledge",
            "row_id": r.get("slug"),
            "slug": r.get("slug"),
            "title": r.get("title", ""),
            "body": r.get("body", ""),
            "host": r.get("updated_by"),
            "actor": "knowledge-curator",
            "created_at": r.get("updated_at"),
            "tags": [r.get("category", "doc")],
            "payload": r.get("metadata"),
        })
    if new_kn:
        state["last_knowledge_at"] = max(r["updated_at"] for r in new_kn)

    # shared_kv — only emit on change (we use updated_at)
    last_kv = state.get("last_kv_at", "1970-01-01T00:00:00Z")
    new_kv = _sb_get("/rest/v1/shared_kv", {
        "select": "*",
        "updated_at": f"gt.{last_kv}",
        "order": "updated_at.asc",
        "limit": "500",
    })
    for r in new_kv:
        rows.append({
            "kind": "kv",
            "row_id": r["k"],
            "slug": f"kv/{r['k']}",
            "title": r["k"],
            "body": json.dumps(r["v"], ensure_ascii=False),
            "host": r.get("updated_by"),
            "actor": "operator",
            "created_at": r.get("updated_at"),
            "tags": ["kv"],
            "payload": None,
        })
    if new_kv:
        state["last_kv_at"] = max(r["updated_at"] for r in new_kv)
    return rows


def push_to_hf(rows: list[dict]) -> bool:
    """Append a JSONL chunk to the HF dataset. Best-effort."""
    if not rows or not HF_TOKEN:
        return False
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"chunk-{ts}-{len(rows)}.jsonl"
    fpath = STAGE_DIR / fname
    with fpath.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log("ingest", f"  staged {fname} ({len(rows)} rows)")

    # Push via huggingface_hub
    try:
        from huggingface_hub import HfApi
    except ImportError:
        log("ingest", "  ⚠ huggingface_hub not installed — chunk on disk only")
        return False
    try:
        api = HfApi(token=HF_TOKEN)
        api.upload_file(
            path_or_fileobj=str(fpath),
            path_in_repo=f"chunks/{fname}",
            repo_id=HF_DATASET,
            repo_type="dataset",
            commit_message=f"ingest: {len(rows)} rows from shared-context",
        )
        log("ingest", f"  ✓ pushed {fname} → {HF_DATASET}")
        return True
    except Exception as e:
        log("ingest", f"  ⚠ HF upload fail: {type(e).__name__}: {str(e)[:160]}")
        return False


def cycle() -> None:
    if not (SB_URL and SB_KEY):
        log("ingest", "  SUPABASE_URL/SUPABASE_SECRET_KEY not set — sleeping")
        return
    state = load_state()
    rows = fetch_new_rows(state)
    if not rows:
        log("ingest", "  no new rows since last cycle")
        return
    log("ingest",
        f"▸ {len(rows)} new rows "
        f"(memory:{sum(1 for r in rows if r['kind']=='memory')} "
        f"knowledge:{sum(1 for r in rows if r['kind']=='knowledge')} "
        f"kv:{sum(1 for r in rows if r['kind']=='kv')})")
    push_to_hf(rows)
    save_state(state)


def main() -> int:
    log("ingest",
        f"start — poll {POLL_SEC}s, dataset={HF_DATASET}")
    while not _stop:
        try:
            cycle()
        except Exception as e:
            log("ingest", f"⚠ cycle err: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(POLL_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
