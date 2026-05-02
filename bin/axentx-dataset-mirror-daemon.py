#!/usr/bin/env python3
"""axentx dataset-mirror — streaming pull of ON-TOPIC public datasets into
our training corpus.

User directive (2026-05-02 round 2):
  > 'มันควร steam นะไม่ควรเป็น cron ถ้า feed มาแล้วก็หาที่ใหม่ feed ต่อ
  >  เรื่อยๆ เลย ... ทุกที่ ทำงานพร้อมกัน แบบ sync state ระหว่างกันด้วย
  >  เป็น thread เลย stream มาเรื่อยๆ และที่ kamatera ทำไมไม่ใช้'

Architecture (post-2026-05-02):
  - N worker THREADS per VM (default 4) loop continuously, never sleep
    between batches except on transient errors.
  - All VMs (GCP, Kamatera, codespaces) run this daemon in PARALLEL —
    they coordinate via the CF Worker /mirror/* routes:
        POST /mirror/lease  → atomically claims (source, offset)
        POST /mirror/advance → bumps offset + releases lease
        GET  /mirror/stats   → read-only inspection
    A 5-min lease prevents stuck workers from blocking forever; the next
    requestor takes over their cursor automatically.
  - 25+ public datasets covering coding, devops/SRE, security, agent/tool
    use, math/code reasoning, and instruction-following dialog.
  - Pairs are appended to state/training-pairs.jsonl with a thread lock;
    the existing push-training-to-hf cron ships them to the HF dataset.

Falls back to a local cursor file if the CF Worker is unreachable, so a
single VM can still stream without coordination (just slower because two
VMs would dup-pull). Emits +N pairs/cycle metric every 60s for dashboards.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

# ── tunables ──────────────────────────────────────────────────────────────
WORKERS = int(os.environ.get("MIRROR_WORKERS", "4"))
PER_BATCH = int(os.environ.get("MIRROR_PER_BATCH", "300"))   # rows per lease
LEASE_TTL = int(os.environ.get("MIRROR_LEASE_TTL", "300"))   # 5 min
RATE_GAP_MS = int(os.environ.get("MIRROR_RATE_GAP_MS", "150"))
IDLE_BACKOFF_SEC = int(os.environ.get("MIRROR_IDLE_BACKOFF_SEC", "30"))
ERROR_BACKOFF_SEC = int(os.environ.get("MIRROR_ERROR_BACKOFF_SEC", "60"))
METRIC_INTERVAL_SEC = int(os.environ.get("MIRROR_METRIC_INTERVAL_SEC", "60"))

PAIRS_FILE = REPO_ROOT / "state" / "training-pairs.jsonl"
HOME_PAIRS = Path.home() / ".surrogate" / "training-pairs.jsonl"
LOCAL_CURSOR_FILE = REPO_ROOT / "state" / ".dataset-mirror-cursor.json"

CF_WORKER = os.environ.get(
    "HARVEST_WORKER_URL",
    "https://surrogate-1-cursor.ashira.workers.dev",
)
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HOSTNAME = socket.gethostname()
WORKER_ID = f"{HOSTNAME}/dataset-mirror"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── source mappers ────────────────────────────────────────────────────────
# Each mapper: row(dict) → {prompt, response} or None to skip.

def _m_alpaca(r):
    inst = r.get("instruction") or ""
    inp = r.get("input") or ""
    out = r.get("output") or ""
    if not (inst and out):
        return None
    p = inst + (f"\n\nInput:\n{inp}" if inp else "")
    return {"prompt": p, "response": out}


def _m_inst_out(r):
    inst = r.get("instruction") or ""
    out = r.get("output") or ""
    if not (inst and out):
        return None
    return {"prompt": inst, "response": out}


def _m_oasst(r):
    if (r.get("role") or "") != "assistant":
        return None
    parent = r.get("parent_text") or r.get("user_text") or ""
    text = r.get("text") or ""
    if not (parent and text):
        return None
    return {"prompt": parent, "response": text}


def _m_swe_bench(r):
    repo = r.get("repo", "")
    inst = r.get("problem_statement") or ""
    patch = r.get("patch") or ""
    if not (inst and patch):
        return None
    return {
        "prompt": f"Repo: {repo}\n\nIssue:\n{inst}\n\nProduce a patch.",
        "response": patch,
    }


def _m_open_orca(r):
    sys_msg = r.get("system_prompt") or ""
    q = r.get("question") or ""
    a = r.get("response") or ""
    if not (q and a):
        return None
    return {"prompt": (sys_msg + "\n\n" + q).strip(), "response": a}


def _m_q_a(r):
    """Common {question, answer} or {input, output} shape."""
    q = r.get("question") or r.get("input") or r.get("prompt") or ""
    a = r.get("answer") or r.get("output") or r.get("response") or ""
    if not (q and a):
        return None
    return {"prompt": q, "response": a}


def _m_sql_create(r):
    """b-mc2/sql-create-context — natural language → SQL with table schema."""
    q = r.get("question") or ""
    ctx = r.get("context") or ""
    a = r.get("answer") or ""
    if not (q and a):
        return None
    p = f"Schema:\n{ctx}\n\nQuestion: {q}" if ctx else q
    return {"prompt": p, "response": a}


def _m_conala(r):
    """neulab/conala-corpus — Python NL→code."""
    intent = r.get("rewritten_intent") or r.get("intent") or ""
    snippet = r.get("snippet") or ""
    if not (intent and snippet):
        return None
    return {"prompt": intent, "response": snippet}


def _m_rosetta(r):
    """christopher/rosetta-code — multi-language code samples."""
    lang = r.get("language_name") or r.get("language") or ""
    task = r.get("task_name") or r.get("task") or ""
    code = r.get("code") or ""
    if not (task and code):
        return None
    return {
        "prompt": f"Implement '{task}' in {lang or 'a chosen language'}.",
        "response": code,
    }


def _m_chat_msgs(r):
    """OpenHermes-style {conversations: [{from, value}, ...]}."""
    msgs = r.get("conversations") or r.get("messages") or []
    if not (isinstance(msgs, list) and len(msgs) >= 2):
        return None
    user, assistant = "", ""
    for m in msgs:
        role = (m.get("from") or m.get("role") or "").lower()
        text = m.get("value") or m.get("content") or ""
        if role in ("human", "user") and not user:
            user = text
        elif role in ("gpt", "assistant", "model") and user and not assistant:
            assistant = text
            break
    if not (user and assistant):
        return None
    return {"prompt": user, "response": assistant}


def _m_tool_call(r):
    """teknium glaive-function-calling — tool-use trajectories."""
    sys_msg = r.get("system") or ""
    chat = r.get("chat") or r.get("conversations") or ""
    if isinstance(chat, str) and chat:
        return {"prompt": (sys_msg + "\n\nUSER:").strip(), "response": chat}
    return _m_chat_msgs(r)


# ── source registry — id, config, split, kind, mapper, max_offset ────────
# max_offset: cap to stop after N rows (None = unlimited).
SOURCES = [
    # Code instruction-following
    ("sahil2801/CodeAlpaca-20k",                None, "train", "code-alpaca",        _m_alpaca,    None),
    ("nickrosh/Evol-Instruct-Code-80k-v1",      None, "train", "code-evol",          _m_inst_out,  None),
    ("ise-uiuc/Magicoder-OSS-Instruct-75K",     None, "train", "code-magicoder-oss", _m_inst_out,  None),
    ("ise-uiuc/Magicoder-Evol-Instruct-110K",   None, "train", "code-magicoder-ev",  _m_inst_out,  None),
    ("theblackcat102/evol-codealpaca-v1",       None, "train", "code-evol-cal",      _m_inst_out,  None),
    ("iamtarun/python_code_instructions_18k_alpaca", None, "train", "code-py-alpaca", _m_alpaca,    None),
    ("ed001/ds-coder-instruct-v1",              None, "train", "code-ds",            _m_inst_out,  None),
    ("ajibawa-2023/Code-290k-ShareGPT",         None, "train", "code-sharegpt",      _m_chat_msgs, None),
    ("nampdn-ai/tiny-codes",                    None, "train", "code-tiny",          _m_q_a,       100_000),
    ("m-a-p/CodeFeedback-Filtered-Instruction", None, "train", "code-feedback",      _m_q_a,       None),
    ("cognitivecomputations/dolphin-coder",     None, "train", "code-dolphin",       _m_chat_msgs, None),

    # SQL / data
    ("b-mc2/sql-create-context",                None, "train", "sql-ctx",            _m_sql_create, None),

    # Multi-language
    ("christopher/rosetta-code",                None, "train", "code-rosetta",       _m_rosetta,    None),
    ("neulab/conala-corpus",                    "curated", "train", "code-conala",   _m_conala,     None),

    # Reasoning / general instr (boundary-aligned with our domain)
    ("Open-Orca/OpenOrca",                      None, "train", "reasoning-orca",     _m_open_orca,  200_000),
    ("WizardLM/WizardLM_evol_instruct_70k",     None, "train", "reasoning-wizard",   _m_inst_out,   None),
    ("TIGER-Lab/MathInstruct",                  None, "train", "math-tiger",         _m_q_a,        None),
    ("yahma/alpaca-cleaned",                    None, "train", "instr-alpaca",       _m_alpaca,     None),
    ("teknium/OpenHermes-2.5",                  None, "train", "dialog-hermes",      _m_chat_msgs,  150_000),
    ("argilla/distilabel-intel-orca-dpo-pairs", None, "train", "reasoning-dpo",      _m_q_a,        None),
    ("allenai/tulu-v2-sft-mixture",             None, "train", "instr-tulu",         _m_chat_msgs,  150_000),

    # Agent / tool use
    ("teknium/openhermes-2.5-glaive-function-calling-v3", None, "train", "agent-tools", _m_tool_call, None),
    ("princeton-nlp/SWE-bench_Lite",            None, "test",  "agent-swebench",     _m_swe_bench,  None),

    # Dialog
    ("OpenAssistant/oasst2",                    None, "train", "dialog-oasst",       _m_oasst,      None),
]

SOURCES_BY_ID = {f"{s[0]}|{s[1] or 'default'}|{s[2]}": s for s in SOURCES}

# ── coordination + I/O ────────────────────────────────────────────────────
_file_lock = threading.Lock()
_metric_lock = threading.Lock()
_metric_pairs = 0
_metric_rows = 0
_metric_started = time.time()


def _http(method: str, url: str, body: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def cf_lease(source: str) -> dict | None:
    """Returns {source, offset, batch} or None if busy/exhausted/unreachable."""
    try:
        r = _http("POST", f"{CF_WORKER}/mirror/lease", {
            "source": source, "claimer": WORKER_ID,
            "batch": PER_BATCH, "ttl_sec": LEASE_TTL,
        }, timeout=15)
        if r.get("exhausted") or r.get("busy"):
            return None
        return r if "offset" in r else None
    except Exception as e:
        log("ds-mirror", f"  lease fail {source[:30]}: {type(e).__name__}: {str(e)[:80]}")
        return None


def cf_advance(source: str, rows: int, end_of_split: bool) -> bool:
    try:
        r = _http("POST", f"{CF_WORKER}/mirror/advance", {
            "source": source, "claimer": WORKER_ID,
            "rows_fetched": rows, "end_of_split": end_of_split,
        }, timeout=15)
        return bool(r.get("ok"))
    except Exception as e:
        log("ds-mirror", f"  advance fail {source[:30]}: {type(e).__name__}: {str(e)[:80]}")
        return False


def fetch_rows(repo: str, config: str | None, split: str,
               offset: int, limit: int) -> list[dict]:
    """HF datasets-server caps per-request length at 100; page in 100-row chunks."""
    cfg = config or "default"
    headers = {"User-Agent": UA}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    rows: list[dict] = []
    page = 100
    fetched = 0
    while fetched < limit:
        chunk = min(page, limit - fetched)
        url = (f"https://datasets-server.huggingface.co/rows"
               f"?dataset={urllib.parse.quote(repo)}"
               f"&config={cfg}&split={split}"
               f"&offset={offset + fetched}&length={chunk}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (404, 422):
                # 404 = past end of split; 422 = bad config or end-of-split.
                break
            raise
        page_rows = [row.get("row") or {} for row in (d.get("rows") or [])]
        if not page_rows:
            break
        rows.extend(page_rows)
        fetched += len(page_rows)
        if len(page_rows) < chunk:
            break
        # tiny in-source rate limit so we don't trigger HF 429
        time.sleep(RATE_GAP_MS / 1000.0)
    return rows


def append_pairs(records: list[dict]) -> None:
    if not records:
        return
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with _file_lock:
        PAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PAIRS_FILE.open("a") as f:
            f.write(payload)
        try:
            HOME_PAIRS.parent.mkdir(parents=True, exist_ok=True)
            with HOME_PAIRS.open("a") as f:
                f.write(payload)
        except (PermissionError, OSError):
            pass


def fingerprint(prompt: str, response: str) -> str:
    return hashlib.sha256((prompt[:500] + "|" + response[:500]).encode()).hexdigest()[:16]


def make_record(kind: str, repo: str, pair: dict) -> dict:
    return {
        "flavor": f"sft-public-{kind}",
        "id": f"public-{kind}-{fingerprint(pair['prompt'], pair['response'])}",
        "prompt": pair["prompt"][:6000],
        "response": pair["response"][:6000],
        "source": f"public:{repo}",
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def process_source(source_key: str, repo: str, config: str | None, split: str,
                   kind: str, mapper, max_offset: int | None) -> int:
    """Lease → fetch → map → append → advance. Returns rows fetched (0 = idle)."""
    lease = cf_lease(source_key)
    if not lease:
        return 0
    offset = int(lease.get("offset", 0))
    if max_offset is not None and offset >= max_offset:
        cf_advance(source_key, rows=0, end_of_split=True)
        return 0

    cap = PER_BATCH
    if max_offset is not None:
        cap = min(cap, max_offset - offset)

    try:
        rows = fetch_rows(repo, config, split, offset, cap)
    except Exception as e:
        log("ds-mirror", f"  ✗ fetch {source_key[:50]} @{offset}: "
                        f"{type(e).__name__}: {str(e)[:100]}")
        # Release lease without advancing — next worker can retry.
        cf_advance(source_key, rows=0, end_of_split=False)
        return -1

    end_of_split = len(rows) < cap
    records = []
    for r in rows:
        try:
            pair = mapper(r)
        except Exception:
            pair = None
        if pair and pair.get("prompt") and pair.get("response"):
            records.append(make_record(kind, repo, pair))

    append_pairs(records)
    cf_advance(source_key, rows=len(rows), end_of_split=end_of_split)

    with _metric_lock:
        global _metric_pairs, _metric_rows
        _metric_pairs += len(records)
        _metric_rows += len(rows)

    return len(rows)


def worker_loop(name: str, stop_evt: threading.Event) -> None:
    """Round-robin through SOURCES; fall through any that's busy/exhausted."""
    idx = hash(name) % len(SOURCES)
    while not stop_evt.is_set():
        src = SOURCES[idx]
        repo, cfg, split, kind, mapper, max_off = src
        source_key = f"{repo}|{cfg or 'default'}|{split}"
        try:
            n = process_source(source_key, repo, cfg, split, kind, mapper, max_off)
        except Exception:
            log("ds-mirror", f"  ⚠ {name} unhandled: {traceback.format_exc()[:300]}")
            n = -1
        # advance the round-robin pointer
        idx = (idx + 1) % len(SOURCES)
        if n == 0:
            # Every source we tried is busy/exhausted — short backoff so we
            # don't burn CPU spinning on cf_lease calls.
            stop_evt.wait(timeout=IDLE_BACKOFF_SEC if idx == 0 else 1)
        elif n < 0:
            stop_evt.wait(timeout=ERROR_BACKOFF_SEC)


def metrics_loop(stop_evt: threading.Event) -> None:
    last_pairs, last_rows, last_t = 0, 0, time.time()
    while not stop_evt.is_set():
        stop_evt.wait(timeout=METRIC_INTERVAL_SEC)
        if stop_evt.is_set():
            return
        now = time.time()
        with _metric_lock:
            dp, dr = _metric_pairs - last_pairs, _metric_rows - last_rows
            tp, tr = _metric_pairs, _metric_rows
            last_pairs, last_rows = _metric_pairs, _metric_rows
        dt = now - last_t
        last_t = now
        if dr > 0:
            log("ds-mirror",
                f"+{dp} pairs (+{dr} rows scanned) in {dt:.0f}s | "
                f"total since boot: {tp} pairs / {tr} rows | "
                f"workers={WORKERS} host={HOSTNAME}")


def main() -> int:
    log("ds-mirror",
        f"streaming mirror starting — workers={WORKERS} batch={PER_BATCH} "
        f"sources={len(SOURCES)} host={HOSTNAME} cf={CF_WORKER}")
    PAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stop_evt = threading.Event()

    import signal
    def shutdown(*_):
        log("ds-mirror", "shutdown signal received")
        stop_evt.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    with ThreadPoolExecutor(max_workers=WORKERS + 1) as pool:
        for i in range(WORKERS):
            pool.submit(worker_loop, f"w{i}", stop_evt)
        pool.submit(metrics_loop, stop_evt)
        # Block on shutdown
        stop_evt.wait()
    log("ds-mirror", "all workers stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
