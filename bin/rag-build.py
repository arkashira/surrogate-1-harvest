#!/usr/bin/env python3
"""rag-build — index decisions + lessons + skills into Cloudflare Vectorize.

Walks:
  state/swarm-shared/decisions/         (agent dev decisions)
  data/memory/lessons_learned.md        (chronological lessons)
  data/memory/knowledge_index.md        (pattern → solution)
  data/skills/**/SKILL.md               (skill library)
  data/knowledge/**/*.md                (Obsidian knowledge files)

For each markdown file, splits into ~500-token chunks, embeds via Workers AI
(@cf/baai/bge-base-en-v1.5 → 768-dim cosine), upserts into Vectorize index
'surrogate-1-rag' with metadata (source path, chunk_idx, project, tags).

Idempotent — uses sha256(path|chunk) as the vector id, so re-runs upsert
unchanged chunks (CF Vectorize is upsert-by-id).

Usage:
  python3 rag-build.py            # full reindex (capped at MAX_CHUNKS)
  python3 rag-build.py --since N  # only files modified in last N seconds
  python3 rag-build.py --query Q  # query mode: returns top-5 hits

Daemon mode: schedule via hermes-jobs.json every 30 min so new agent
decisions get indexed within minutes of being written.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CF_TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
CF_ACCT = os.environ["CLOUDFLARE_ACCOUNT_ID"]
INDEX_NAME = "surrogate-1-rag"
EMB_MODEL = "@cf/baai/bge-base-en-v1.5"
EMB_DIMS = 768

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
MAX_CHUNKS = int(os.environ.get("RAG_MAX_CHUNKS", "2000"))
CHUNK_CHARS = int(os.environ.get("RAG_CHUNK_CHARS", "1800"))  # ~500 tokens
CHUNK_OVERLAP = 200

CURSOR_FILE = REPO_ROOT / "state" / ".rag-cursor.json"
LOG_FILE = REPO_ROOT / "logs" / "rag-build.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def cf(method: str, path: str, body=None, raw_body=False) -> dict:
    url = f"https://api.cloudflare.com/client/v4{path}"
    headers = {"Authorization": f"Bearer {CF_TOKEN}"}
    data = None
    if body is not None:
        if raw_body:
            data = body if isinstance(body, bytes) else body.encode()
            headers["Content-Type"] = "application/x-ndjson"
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"success": False, "errors": [{"code": e.code, "message": e.read().decode()[:300]}]}


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch (CF AI takes up to 100 texts per call)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        r = cf("POST", f"/accounts/{CF_ACCT}/ai/run/{EMB_MODEL}", {"text": batch})
        if not r.get("success"):
            raise RuntimeError(f"embed fail: {r.get('errors')}")
        for v in r["result"]["data"]:
            out.append(v)
    return out


def chunk_md(text: str) -> list[str]:
    """Crude paragraph-aware chunker — fits under 500 tokens per chunk."""
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        # back off to nearest newline so we don't cut mid-paragraph
        if end < len(text):
            nl = text.rfind("\n\n", start, end)
            if nl > start + CHUNK_CHARS // 2:
                end = nl
        chunks.append(text[start:end].strip())
        start = end - CHUNK_OVERLAP if end < len(text) else end
    return [c for c in chunks if c]


def collect_corpus(since_ts: float = 0) -> list[tuple[str, str]]:
    """Return list of (id, text) pairs to index."""
    sources: list[tuple[Path, str, dict]] = []

    # 1. swarm-shared/decisions
    decisions_dir = REPO_ROOT / "state" / "swarm-shared" / "decisions"
    if decisions_dir.exists():
        for p in decisions_dir.glob("*.md"):
            if p.stat().st_mtime >= since_ts:
                sources.append((p, "decision", {"kind": "decision"}))

    # 2. data/memory/*.md
    mem_dir = REPO_ROOT / "data" / "memory"
    if mem_dir.exists():
        for p in mem_dir.glob("*.md"):
            if p.stat().st_mtime >= since_ts:
                sources.append((p, "memory", {"kind": "memory"}))

    # 3. data/skills/**/SKILL.md
    skills_dir = REPO_ROOT / "data" / "skills"
    if skills_dir.exists():
        for p in skills_dir.rglob("SKILL.md"):
            if p.stat().st_mtime >= since_ts:
                sources.append((p, "skill", {"kind": "skill"}))

    # 4. data/knowledge/**/*.md
    know_dir = REPO_ROOT / "data" / "knowledge"
    if know_dir.exists():
        for p in know_dir.rglob("*.md"):
            if p.stat().st_mtime >= since_ts:
                sources.append((p, "knowledge", {"kind": "knowledge"}))

    pairs: list[tuple[str, str, dict]] = []
    for path, _kind, meta in sources:
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for i, chunk in enumerate(chunk_md(text)):
            vid = hashlib.sha256(f"{rel}|{i}".encode()).hexdigest()[:32]
            chunk_meta = dict(meta, source=rel, chunk_idx=i)
            pairs.append((vid, chunk, chunk_meta))

    # Cap so we never spend more than ~MAX_CHUNKS embed calls in one run
    if len(pairs) > MAX_CHUNKS:
        log(f"  capping {len(pairs)} chunks to {MAX_CHUNKS} (most recent first)")
        pairs.sort(key=lambda t: -((REPO_ROOT / t[2]["source"]).stat().st_mtime
                                   if (REPO_ROOT / t[2]["source"]).exists() else 0))
        pairs = pairs[:MAX_CHUNKS]
    return pairs


def upsert_vectors(pairs: list[tuple[str, str, dict]]) -> int:
    """Embed chunks then NDJSON-upsert to Vectorize. Returns count upserted."""
    if not pairs:
        return 0
    texts = [p[1] for p in pairs]
    log(f"  embedding {len(texts)} chunks…")
    vecs = embed_batch(texts)

    # NDJSON: one vector per line {id, values, metadata}
    lines = []
    for (vid, _txt, meta), values in zip(pairs, vecs):
        lines.append(json.dumps({"id": vid, "values": values, "metadata": meta}))
    body = "\n".join(lines) + "\n"

    log(f"  upserting {len(lines)} vectors → {INDEX_NAME}…")
    r = cf("POST",
           f"/accounts/{CF_ACCT}/vectorize/v2/indexes/{INDEX_NAME}/upsert",
           body, raw_body=True)
    if not r.get("success"):
        raise RuntimeError(f"upsert fail: {r.get('errors')}")
    log(f"  ✅ upserted: {r.get('result')}")
    return len(lines)


def query(question: str, top_k: int = 5) -> list[dict]:
    """Query mode: embed question, return top_k hits."""
    qvec = embed_batch([question])[0]
    r = cf("POST",
           f"/accounts/{CF_ACCT}/vectorize/v2/indexes/{INDEX_NAME}/query",
           {"vector": qvec, "topK": top_k, "returnMetadata": "all", "returnValues": False})
    if not r.get("success"):
        raise RuntimeError(f"query fail: {r.get('errors')}")
    return r["result"]["matches"]


def main():
    if "--query" in sys.argv:
        i = sys.argv.index("--query")
        q = " ".join(sys.argv[i + 1:])
        if not q:
            print("usage: rag-build.py --query <question>")
            return 1
        for hit in query(q):
            print(f"  {hit['score']:.3f}  {hit['metadata'].get('source','?')}  "
                  f"chunk={hit['metadata'].get('chunk_idx','?')}")
        return 0

    since_ts = 0.0
    if "--since" in sys.argv:
        since_ts = (datetime.datetime.utcnow().timestamp()
                    - int(sys.argv[sys.argv.index("--since") + 1]))

    log(f"rag-build start  since_ts={since_ts}  index={INDEX_NAME}")
    pairs = collect_corpus(since_ts)
    log(f"  collected {len(pairs)} chunks")
    if not pairs:
        log("  nothing to index"); return 0
    n = upsert_vectors(pairs)
    log(f"rag-build done — {n} vectors upserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
