#!/usr/bin/env python3
"""axentx HF flusher — continuously drains the D1 staging buffer to HF.

User directive (2026-05-02 round 2):
  > 'มันควร steam นะไม่ควรเป็น cron'

Old shape: cron-style, sleep 15 min between cycles.
New shape: streaming. Drain in a tight loop with adaptive backoff:
  - Buffer empty       → wait 30s
  - Push success       → 1s gap (next batch immediately), bigger batch
  - Push 429 / 5xx     → exponential backoff up to 10 min, smaller batch

Architecture:
  research-daemon (any VM) → POST /harvest/post → D1 harvested_pains table
                                                   ↓ (streaming)
                                       this flusher → HF Datasets repo

  When HF rate-limits us, staging in D1 keeps the raw posts safe. Flusher
  retries with backoff + adaptive batch sizes. No data loss.

Target HF dataset: axentx/surrogate-1-harvested-pains
  Schema: source, url, title, body, score, harvested_at
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

# ── tunables ──────────────────────────────────────────────────────────────
EMPTY_GAP_SEC = int(os.environ.get("HF_FLUSHER_EMPTY_GAP", "30"))   # buffer empty
SUCCESS_GAP_SEC = int(os.environ.get("HF_FLUSHER_SUCCESS_GAP", "1"))
MIN_BATCH = int(os.environ.get("HF_FLUSHER_MIN_BATCH", "100"))
MAX_BATCH = int(os.environ.get("HF_FLUSHER_MAX_BATCH", "1000"))
INITIAL_BATCH = int(os.environ.get("HF_FLUSHER_BATCH", "300"))
MAX_BACKOFF_SEC = int(os.environ.get("HF_FLUSHER_MAX_BACKOFF", "600"))

HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_DATASET = os.environ.get(
    "HF_HARVEST_DATASET",
    "axentx/surrogate-1-harvested-pains",
)

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CF_ACCT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
DB_ID = os.environ.get("D1_DATABASE_ID", "ae95ac58-7b7e-40d9-8708-518c23281ae6")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_stop_evt = False


def _on_signal(*_):
    global _stop_evt
    _stop_evt = True
    log("hf-flusher", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _d1_query(sql: str, params: list | None = None) -> dict:
    if not (CF_TOKEN and CF_ACCT and DB_ID):
        return {}
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCT}"
           f"/d1/database/{DB_ID}/query")
    body = {"sql": sql}
    if params:
        body["params"] = params
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 method="POST", headers={
                                     "Authorization": f"Bearer {CF_TOKEN}",
                                     "Content-Type": "application/json",
                                 })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log("hf-flusher", f"  d1 query fail: {type(e).__name__}: {str(e)[:120]}")
        return {}


# Migrated 2026-05-03 from D1 (CF) → Supabase. CF free tier rate-limit (1027)
# was killing all coordination traffic at scale. Supabase PostgREST is
# unlimited for table-direct ops on the free tier.
SB_URL = os.environ.get("SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get(
    "SUPABASE_SERVICE_KEY", ""
)
_SB_HEADERS = {
    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json", "User-Agent": UA,
}


def _sb_get(path: str, timeout: int = 15) -> list:
    if not (SB_URL and SB_KEY):
        return []
    try:
        req = urllib.request.Request(
            f"{SB_URL}{path}", method="GET", headers=_SB_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()) or []
    except Exception as e:
        log("hf-flusher", f"  sb GET fail: {type(e).__name__}: {str(e)[:120]}")
        return []


def fetch_pending(limit: int) -> list[dict]:
    """Pull pending rows. Supabase first; fallback to D1 if Supabase down."""
    if SB_URL and SB_KEY:
        # PostgREST: GET /rest/v1/harvested_pains?pushed_to_hf=eq.0&order=harvested_at.asc&limit=N
        rows = _sb_get(
            f"/rest/v1/harvested_pains?pushed_to_hf=eq.0"
            f"&order=harvested_at.asc&limit={limit}"
            f"&select=id,source,url,title,body,score,harvested_at"
        )
        if rows:
            return rows
        # Empty result from Supabase = no pending; fall through to D1 only
        # if Supabase explicitly errored (rows=[] both for empty + error).
        # We can't distinguish — but this is harmless; D1 will return [] too
        # if it's also empty.
    r = _d1_query(
        "SELECT id, source, url, title, body, score, harvested_at "
        "FROM harvested_pains WHERE pushed_to_hf = 0 "
        "ORDER BY harvested_at ASC LIMIT ?", [limit],
    )
    try:
        return (r.get("result") or [{}])[0].get("results") or []
    except Exception:
        return []


def mark_pushed(ids: list[int]) -> None:
    if not ids:
        return
    # Supabase: DELETE /rest/v1/harvested_pains?id=in.(1,2,3,...)
    if SB_URL and SB_KEY:
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            id_list = ",".join(str(int(x)) for x in chunk)
            try:
                req = urllib.request.Request(
                    f"{SB_URL}/rest/v1/harvested_pains?id=in.({id_list})",
                    method="DELETE",
                    headers={**_SB_HEADERS, "Prefer": "return=minimal"},
                )
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                log("hf-flusher",
                    f"  ⚠ sb delete chunk {i//50} failed ({type(e).__name__}); "
                    "row stays for retry next cycle (idempotent)")
        return
    # Legacy D1 fallback
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        placeholders = ",".join("?" for _ in chunk)
        r = _d1_query(
            f"DELETE FROM harvested_pains WHERE id IN ({placeholders})",
            [int(x) for x in chunk],
        )
        if not r:
            log("hf-flusher", f"  ⚠ d1 delete chunk {i//50} failed — will reflush")


def push_to_hf(rows: list[dict]) -> str:
    """Push a batch as JSONL via huggingface_hub. Returns:
         "ok"          — pushed
         "rate"        — HF rate-limited; back off
         "retry"       — transient error; back off less
         "skip"        — config error (no token, etc.); skip cycle
    """
    if not HF_TOKEN or not rows:
        return "skip"
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.utils import HfHubHTTPError
    except ImportError:
        log("hf-flusher", "  ⚠ huggingface_hub missing; pip install in venv")
        return "skip"
    ndjson = "\n".join(json.dumps({
        "source": r.get("source", ""),
        "url": r.get("url", ""),
        "title": r.get("title", ""),
        "body": r.get("body", ""),
        "score": r.get("score", 0),
        "harvested_at": r.get("harvested_at", 0),
    }, ensure_ascii=False) for r in rows) + "\n"
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"data/{ts}-{len(rows):04d}.jsonl"
    api = HfApi(token=HF_TOKEN)
    try:
        api.upload_file(
            path_or_fileobj=ndjson.encode("utf-8"),
            path_in_repo=fname,
            repo_id=HF_DATASET,
            repo_type="dataset",
            commit_message=f"flush: +{len(rows)} pains @ {ts}",
        )
        log("hf-flusher", f"  ✓ pushed {len(rows)} → {fname}")
        return "ok"
    except HfHubHTTPError as e:
        code = getattr(e.response, "status_code", 0) if getattr(e, "response", None) else 0
        if code == 429 or code in (502, 503):
            log("hf-flusher", f"  ⚠ HF rate/5xx ({code}) — backing off")
            return "rate"
        log("hf-flusher", f"  ✗ HfHub {code}: {str(e)[:160]}")
        return "retry"
    except Exception as e:
        log("hf-flusher", f"  ✗ HF push fail: {type(e).__name__}: {str(e)[:160]}")
        return "retry"


def main() -> int:
    log("hf-flusher",
        f"streaming flusher starting — initial batch={INITIAL_BATCH} → "
        f"{HF_DATASET}")
    batch = INITIAL_BATCH
    backoff = 0
    while not _stop_evt:
        if backoff > 0:
            for _ in range(backoff):
                if _stop_evt:
                    return 0
                time.sleep(1)
            backoff = 0

        rows = fetch_pending(batch)
        if not rows:
            time.sleep(EMPTY_GAP_SEC)
            continue

        result = push_to_hf(rows)
        if result == "ok":
            mark_pushed([r["id"] for r in rows])
            # accelerate: bigger batch on success, short gap
            batch = min(int(batch * 1.5), MAX_BATCH)
            time.sleep(SUCCESS_GAP_SEC)
        elif result == "rate":
            # HF rate-limited: shrink batch, exponential backoff
            batch = max(int(batch / 2), MIN_BATCH)
            backoff = min(max(backoff * 2, 60), MAX_BACKOFF_SEC)
            log("hf-flusher", f"  back off {backoff}s, batch→{batch}")
        elif result == "retry":
            backoff = min(max(backoff * 2, 30), 300)
        else:  # skip
            time.sleep(EMPTY_GAP_SEC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
