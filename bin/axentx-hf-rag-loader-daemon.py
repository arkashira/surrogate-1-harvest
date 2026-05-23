#!/usr/bin/env python3
"""axentx hf-rag-loader — pulls high-value rows from the
`axentx/surrogate-1-training-pairs` HF dataset back into shared_knowledge,
so EVERY agent can retrieve learned patterns before calling the LLM.

User feedback 2026-05-04:
  > 'แล้ว dataset บนHF เอามาใช้ให้ agent ตอนนี้เก่งขึ้นได้ไหม.
  >  และทุกอย่างต้อง ingest กลับไปที่ HF dataset นะ'

Bidirectional sync (closed loop):
  ─────────────────── PUSH up (existing) ──────────────────────
  knowledge-ingest-daemon → JSONL chunks → axentx/surrogate-1-training-pairs

  ─────────────────── PULL down (THIS daemon) ─────────────────
  HF dataset chunks → filter top patterns → shared_knowledge entries
  ↓
  Any agent can query shared_knowledge by topic/pattern → injects
  retrieved examples into LLM prompt as 'few-shot context' → smarter
  decisions without fine-tuning.

Cycle (every 6 hours — dataset doesn't change fast):
  1. Resolve dataset latest revision via HF API
  2. List JSONL files (one per ingest day)
  3. Pull last 7 days of files (limit 5MB total to control bandwidth)
  4. Parse rows; filter: kind in {fix, milestone, deployed, build-fail,
     env-drift, auth-fail, heal-stale-agent, snapshot}
  5. Group rows by 'kind' + cluster by simple keyword overlap
  6. For each cluster ≥3 rows → write 1 shared_knowledge entry
     slug='hf-pattern/<kind>/<cluster_id>' category='hf-pattern'
     body=summarized lessons (best-effort: top N rows verbatim)
  7. Bonus: track hash of dataset latest commit in shared_kv to skip work
     if nothing changed.

Cost discipline:
  - 6h poll = 4 cycles/day × 5MB = ~20MB/day egress per host
  - Run on leader host only (lowest hostname) — others read shared_knowledge
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from collections import Counter, defaultdict

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("HF_RAG_LOADER_POLL_SEC", "10800"))   # 3 hours
HOST = socket.gethostname()
# Mine BOTH datasets — primary training-pairs + secondary axentx-shared
HF_DATASETS = [
    s.strip() for s in os.environ.get(
        "HF_RAG_DATASETS",
        "axentx/surrogate-1-training-pairs,axentx/shared-context-stream"
    ).split(",") if s.strip()
]
HF_TOKEN = (os.environ.get("HF_TOKEN")
            or os.environ.get("HF_TOKEN_PRO_WRITE", ""))
# Scaled up dramatically (user feedback 2026-05-04: 'มี 10TB ดึงแค่นี้
# ช่วยอะไร'). Per cycle: pull up to 60 files, 100MB total. Sampling
# stratified by kind so we get coverage across all behavior types.
MAX_BYTES_PER_CYCLE = int(os.environ.get("HF_RAG_MAX_BYTES", "104857600"))  # 100MB
MAX_FILES = int(os.environ.get("HF_RAG_MAX_FILES", "60"))   # last 60 days

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _hf_get(url: str, token: str = HF_TOKEN, timeout: int = 60) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": "axentx-hf-rag-loader"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        log("hf-rag-loader", f"  ⚠ fetch {url}: {e}")
        return None


def list_jsonl_files(dataset: str) -> list[tuple[str, int]]:
    """List .jsonl siblings in dataset, sorted desc by name (date)."""
    api = f"https://huggingface.co/api/datasets/{dataset}"
    body = _hf_get(api, timeout=20)
    if not body:
        return []
    try:
        d = json.loads(body)
    except Exception:
        return []
    files: list[tuple[str, int]] = []
    for s in d.get("siblings", []):
        name = s.get("rfilename", "")
        if name.endswith(".jsonl") or name.endswith(".parquet"):
            sz = int(s.get("size", 0))
            files.append((name, sz))
    # Sort by name desc — dataset uses YYYY-MM-DD prefix so newest first
    files.sort(key=lambda x: x[0], reverse=True)
    return files[:MAX_FILES]


def fetch_rows(dataset: str, filename: str) -> list[dict]:
    raw = f"https://huggingface.co/datasets/{dataset}/raw/main/{filename}"
    # Retry once on SSL handshake timeout (HF CDN flakes)
    body = None
    for attempt in range(2):
        body = _hf_get(raw, timeout=120)
        if body:
            break
    if not body:
        return []
    rows = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


_KEYWORDS_RE = re.compile(r"[a-z]{4,}")


def _cluster_key(row: dict) -> str:
    """Simple hash of top keywords in title+body — rows with overlapping
    keyword sets share a cluster."""
    txt = (row.get("title", "") + " " + row.get("body", "")).lower()
    words = _KEYWORDS_RE.findall(txt)
    common = Counter(words).most_common(5)
    keys = sorted(w for w, _ in common)
    return "_".join(keys[:4])[:60] or "misc"


def summarize_cluster(kind: str, rows: list[dict]) -> tuple[str, str]:
    """(title, body) for a shared_knowledge entry."""
    sample = rows[:5]
    title = f"HF pattern: {kind} — {sample[0].get('title','')[:80]}"
    body_lines = [
        f"# Cluster: {kind} ({len(rows)} occurrences)\n",
        f"Pulled from `{HF_DATASETS[0] if HF_DATASETS else '?'}` on "
        f"{datetime.datetime.utcnow().isoformat()}Z by hf-rag-loader.\n",
        "## Top examples (verbatim from dataset):\n",
    ]
    for i, r in enumerate(sample, 1):
        body_lines.append(
            f"### {i}. [{r.get('host','?')}] {r.get('title','')[:120]}")
        b = (r.get("body") or "")[:600]
        if b:
            body_lines.append(f"```\n{b}\n```")
        body_lines.append("")
    body_lines.append("## How agents should use this:\n")
    body_lines.append(
        f"When facing a similar `{kind}` situation, fetch this entry "
        "via `knowledge_search('{kind}')` and inject the examples as "
        "few-shot context into the LLM prompt. Saves discovery cost.")
    return title, "\n".join(body_lines)


def _knowledge_set(slug: str, category: str, title: str,
                   body: str) -> bool:
    """Push HF knowledge into axentx-coordinator (2026-05-23: replaces
    disabled Supabase RPC). Returns True on 200, False otherwise."""
    coord_url = os.environ.get("XVM_QUEUE_URL", "")
    coord_tok = os.environ.get("COORDINATOR_TOKEN", "")
    if not coord_url:
        return False
    try:
        # Topic key = slug; tags = [category, "hf-rag-loader"]
        full_content = (f"# {title}\n\n{body[:30000]}"
                        if title else body[:30000])
        payload = json.dumps({
            "topic": slug,
            "content": full_content,
            "tags": [category, "hf-rag-loader",
                     (HF_DATASETS[0] if HF_DATASETS else "?")],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if coord_tok:
            headers["Authorization"] = f"Bearer {coord_tok}"
        req = urllib.request.Request(
            f"{coord_url}/knowledge/upsert",
            data=payload, method="POST", headers=headers,
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log("hf-rag-loader", f"  ⚠ knowledge_set({slug}): {e}")
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("hf-rag-loader",
            "  ⤷ not leader (other host owns RAG pull)")
        return False
    if not HF_TOKEN:
        log("hf-rag-loader", "  ⚠ HF_TOKEN missing — skip cycle")
        return False

    bytes_used = 0
    all_rows: list[dict] = []
    files_pulled_total = 0
    for ds in HF_DATASETS:
        files = list_jsonl_files(ds)
        if not files:
            log("hf-rag-loader", f"  ⊘ {ds}: no files visible")
            continue
        log("hf-rag-loader",
            f"  ▸ {ds}: {len(files)} candidate files")
        for name, sz in files:
            if bytes_used + sz > MAX_BYTES_PER_CYCLE:
                break
            rows = fetch_rows(ds, name)
            for r in rows:
                r["_source_dataset"] = ds   # track origin
            all_rows.extend(rows)
            bytes_used += sz
            files_pulled_total += 1
        if bytes_used >= MAX_BYTES_PER_CYCLE:
            log("hf-rag-loader",
                f"  ✓ byte cap reached at {bytes_used:,} — stop pulls")
            break

    if not all_rows:
        log("hf-rag-loader", "  ✓ no rows pulled — done")
        return False

    log("hf-rag-loader",
        f"  ✓ pulled {len(all_rows)} rows from "
        f"{files_pulled_total} files across {len(HF_DATASETS)} datasets "
        f"({bytes_used:,} bytes)")

    # Filter to learning-rich kinds (broaden — user said too narrow)
    interesting_kinds = {
        "fix", "milestone", "deployed", "build-fail", "env-drift",
        "auth-fail", "heal-stale-agent", "snapshot", "lesson", "pattern",
        "verdict-extend", "verdict-pass", "verdict-new-product",
        "synthesized-feature", "synthesized-product", "rerouted",
        "test-result", "test-result-pass", "test-result-fail",
        "scan-finding", "broken-down", "opportunities-refreshed",
        "mined-plans", "verified-skip", "rag-pull",
    }
    rows = [r for r in all_rows
            if (r.get("kind") in interesting_kinds
                or "kind" not in r
                or r.get("kind", "").startswith("verdict-"))]

    # Cluster by kind + keyword sig
    by_cluster: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        kind = r.get("kind", "general")
        ck = _cluster_key(r)
        by_cluster[(kind, ck)].append(r)

    written = 0
    for (kind, ck), rs in by_cluster.items():
        if len(rs) < 2:
            continue
        slug_id = hashlib.md5(f"{kind}/{ck}".encode()).hexdigest()[:10]
        slug = f"hf-pattern/{kind}/{slug_id}"
        title, body = summarize_cluster(kind, rs)
        if _knowledge_set(slug, "hf-pattern", title, body):
            written += 1
            if written >= 200:   # raised from 30 — at 100MB pull we should
                break             # extract many more patterns

    # Also build per-actor (role) summaries so each agent can pull
    # role-specific examples before LLM calls. e.g.
    # shared_knowledge["hf-role/bd"] = top 50 bd verdicts examples
    by_actor: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        a = r.get("actor") or r.get("source") or ""
        if a and len(a) < 40:
            by_actor[a].append(r)
    role_written = 0
    for actor, rs in by_actor.items():
        if len(rs) < 5:
            continue
        # Top 30 examples for this actor (most-recent first)
        rs_sorted = sorted(rs, key=lambda x: x.get("created_at", ""),
                           reverse=True)[:30]
        body_lines = [
            f"# Top {len(rs_sorted)} examples for actor `{actor}` "
            f"(pulled from HF dataset by hf-rag-loader)\n",
            f"Use these to prime LLM calls when this agent runs.\n",
        ]
        for i, r in enumerate(rs_sorted, 1):
            body_lines.append(
                f"## {i}. [{r.get('kind','?')}] {r.get('title','')[:120]}")
            body_lines.append(f"```\n{(r.get('body') or '')[:400]}\n```")
        slug_role = f"hf-role/{actor}"
        if _knowledge_set(slug_role, "hf-role",
                          f"Role context: {actor} ({len(rs_sorted)} examples)",
                          "\n".join(body_lines)):
            role_written += 1

    log("hf-rag-loader",
        f"  ✓ {len(rows)} rows filtered → "
        f"{written} hf-pattern + {role_written} hf-role entries written")

    try:
        from axentx_shared import kv_set, memory_log
        kv_set("hf-rag.last_pull", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "datasets": HF_DATASETS,
            "files_pulled": files_pulled_total,
            "rows_total": len(all_rows),
            "rows_filtered": len(rows),
            "patterns_written": written,
            "role_summaries": role_written,
            "bytes_used": bytes_used,
        })
        memory_log("hf-rag-loader", "rag-pull",
                   f"pulled {len(rows)} rows, wrote {written} patterns + "
                   f"{role_written} role summaries",
                   body=(f"Datasets: {HF_DATASETS}\n"
                         f"Files: {files_pulled_total}\n"
                         f"Bytes: {bytes_used:,}"),
                   tags=["hf-rag-loader", HOST])
    except Exception:
        pass
    return False


if __name__ == "__main__":
    daemon_loop("hf-rag-loader", POLL_SEC, cycle)
