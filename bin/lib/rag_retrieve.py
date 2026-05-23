"""
RAG retrieval — query FTS5 + vector index for similar past work, inject as context.

Hybrid retrieval:
  1. FTS5 keyword match over training-pairs (fast, exact matches)
  2. Optional: vector semantic via nomic-embed-text + sqlite-vec (semantic intent)
  3. Reciprocal rank fusion of both → top-K to inject

Usage from orchestrate's call_agent BEFORE LLM call:
    from rag_retrieve import retrieve_similar
    context = retrieve_similar(prompt, top_k=3, max_kb=10)
    # inject `context` into prompt as 'Similar past work:'

Cache hits within 60s window (avoid repeat queries during multi-stage pipeline).
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Iterable

HOME = Path(os.environ.get("HOME", "/home/hermes"))
FTS_DB = HOME / ".surrogate/state/self-ingest.db"
VEC_DB = HOME / ".surrogate/state/rag-vectors.db"
CACHE_DIR = HOME / ".surrogate/state/rag-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"


def _cache_get(key: str) -> str | None:
    cf = CACHE_DIR / f"{key}.txt"
    if cf.exists() and (time.time() - cf.stat().st_mtime) < 60:
        return cf.read_text()
    return None


def _cache_put(key: str, value: str) -> None:
    cf = CACHE_DIR / f"{key}.txt"
    cf.write_text(value)


def _hash_key(query: str, top_k: int) -> str:
    return hashlib.md5(f"{query[:500]}|{top_k}".encode()).hexdigest()[:12]


def _fts_search(query: str, top_k: int = 5) -> list[tuple[str, str, float, str]]:
    """Returns [(prompt, response, score, source), ...] from FTS5 index."""
    if not FTS_DB.exists():
        return []
    # Sanitize query for FTS5 — extract keywords, drop stopwords
    import re
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b', query)
    stop = {"the", "and", "for", "with", "from", "this", "that", "what",
            "when", "where", "how", "why", "which", "into", "your"}
    keywords = [w for w in words if w.lower() not in stop][:10]
    if not keywords:
        return []
    fts_query = " OR ".join(f'"{kw}"' for kw in keywords)

    try:
        with sqlite3.connect(str(FTS_DB), timeout=3) as c:
            rows = c.execute(
                "SELECT prompt, response, rank, source FROM pairs "
                "WHERE pairs MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, top_k * 2)
            ).fetchall()
        return [(r[0], r[1], -float(r[2]), r[3]) for r in rows[:top_k]]
    except Exception as e:
        print(f"FTS error: {e}", file=__import__("sys").stderr)
        return []


def _embed_query(text: str) -> list[float] | None:
    """Get embedding for a query via Ollama nomic-embed-text."""
    try:
        body = json.dumps({"model": EMBED_MODEL, "prompt": text[:2000]}).encode()
        req = urllib.request.Request(OLLAMA_EMBED_URL, data=body,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r).get("embedding") or None
    except Exception:
        return None


def _vec_search(query_vec: list[float], top_k: int = 5) -> list[tuple[str, str, float, str]]:
    """Vector cosine search via sqlite — fallback to numpy if no sqlite-vec."""
    if not VEC_DB.exists() or not query_vec:
        return []
    try:
        import numpy as np
        with sqlite3.connect(str(VEC_DB), timeout=3) as c:
            rows = c.execute(
                "SELECT prompt, response, embedding, source FROM vectors LIMIT 50000"
            ).fetchall()
        if not rows:
            return []
        q = np.array(query_vec, dtype=np.float32)
        q /= (np.linalg.norm(q) + 1e-9)
        scored: list[tuple[str, str, float, str]] = []
        for prompt, response, emb_blob, src in rows:
            emb = np.frombuffer(emb_blob, dtype=np.float32)
            if emb.shape[0] != q.shape[0]:
                continue
            cos = float(np.dot(q, emb / (np.linalg.norm(emb) + 1e-9)))
            scored.append((prompt, response, cos, src))
        scored.sort(key=lambda x: -x[2])
        return scored[:top_k]
    except Exception as e:
        print(f"Vec search err: {e}", file=__import__("sys").stderr)
        return []


def _fuse(fts: list, vec: list, top_k: int = 3) -> list[tuple[str, str, str, float]]:
    """Reciprocal rank fusion — combine FTS + vec rankings."""
    seen: dict[str, dict] = {}
    for rank, (prompt, response, _, src) in enumerate(fts):
        key = prompt[:100]
        seen.setdefault(key, {"prompt": prompt, "response": response, "source": src,
                              "rrf": 0.0})
        seen[key]["rrf"] += 1.0 / (60 + rank)
    for rank, (prompt, response, _, src) in enumerate(vec):
        key = prompt[:100]
        seen.setdefault(key, {"prompt": prompt, "response": response, "source": src,
                              "rrf": 0.0})
        seen[key]["rrf"] += 1.0 / (60 + rank)
    ranked = sorted(seen.values(), key=lambda x: -x["rrf"])
    return [(r["prompt"], r["response"], r["source"], r["rrf"]) for r in ranked[:top_k]]


def retrieve_similar(query: str, top_k: int = 3, max_kb: int = 10) -> str:
    """Returns markdown-formatted 'Similar past work' block to inject in prompt.
    Empty string if no good matches."""
    if not query or len(query) < 30:
        return ""
    cache_key = _hash_key(query, top_k)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Run both retrievals in parallel (best-effort)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fts_fut = ex.submit(_fts_search, query, top_k)
        # Vec retrieval optional — only if Ollama embeddings available
        vec_fut = ex.submit(lambda: _vec_search(_embed_query(query) or [], top_k))
        try:
            fts_results = fts_fut.result(timeout=5)
        except Exception:
            fts_results = []
        try:
            vec_results = vec_fut.result(timeout=10)
        except Exception:
            vec_results = []

    fused = _fuse(fts_results, vec_results, top_k)
    if not fused:
        _cache_put(cache_key, "")
        return ""

    out_parts = ["### Similar past work (from training-pairs.jsonl):\n"]
    budget = max_kb * 1024
    for i, (p, r, src, score) in enumerate(fused, 1):
        chunk = f"\n#### Match {i} (source: {src}, score: {score:.3f})\n"
        chunk += f"**Q:** {p[:600]}\n"
        chunk += f"**A:** {r[:1200]}\n"
        if len(chunk) > budget:
            break
        out_parts.append(chunk)
        budget -= len(chunk)

    out = "".join(out_parts)
    _cache_put(cache_key, out)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: rag_retrieve.py <query>", file=sys.stderr)
        sys.exit(2)
    q = " ".join(sys.argv[1:])
    print(retrieve_similar(q, top_k=3))
