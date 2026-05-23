"""axentx_semantic_cache — prompt-hash cache + cross-host shared via Supabase.

Cuts repeated identical-prompt LLM calls. With 80 dev daemons hitting
similar prompts (same project + similar focus), 30-50% hit rate is real.

Per research note (ModelCache/GPTCache): semantic cache (embedding-based)
is even better, but requires embedding model. Start with prompt-hash —
free, instant, no embedding cost.

Usage:
    from axentx_semantic_cache import cache_get, cache_set
    key = (system_prompt, user_prompt, model)
    cached = cache_get(key)
    if cached: return cached
    response = call_llm(...)
    cache_set(key, response, ttl_sec=3600)
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import time

# In-process LRU — avoids supabase round-trip for hot cache
_INPROC: dict[str, tuple[float, str]] = {}
_INPROC_MAX = 500


def _hash_key(key: tuple) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()[:24]


def cache_get(key: tuple) -> str | None:
    h = _hash_key(key)
    # In-process first
    entry = _INPROC.get(h)
    if entry:
        expires, value = entry
        if expires > time.time():
            return value
        _INPROC.pop(h, None)
    # Cross-host via shared_kv
    try:
        from axentx_shared import kv_get
        v = kv_get(f"semcache.{h}")
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        if not isinstance(v, dict):
            return None
        if v.get("expires", 0) > int(time.time()):
            response = v.get("response")
            if response:
                # Promote to in-process
                _INPROC[h] = (v["expires"], response)
                if len(_INPROC) > _INPROC_MAX:
                    _INPROC.pop(next(iter(_INPROC)))
                return response
    except Exception:
        pass
    return None


def cache_set(key: tuple, response: str, ttl_sec: int = 3600) -> None:
    if not response or len(response) > 100_000:
        return
    h = _hash_key(key)
    expires = int(time.time()) + ttl_sec
    _INPROC[h] = (expires, response)
    if len(_INPROC) > _INPROC_MAX:
        _INPROC.pop(next(iter(_INPROC)))
    try:
        from axentx_shared import kv_set
        kv_set(f"semcache.{h}", {
            "expires": expires,
            "response": response[:50000],
            "stored_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
    except Exception:
        pass


def cache_stats() -> dict:
    return {"inproc_entries": len(_INPROC),
            "inproc_max": _INPROC_MAX}


__all__ = ["cache_get", "cache_set", "cache_stats",
           "near_get", "near_set", "near_stats"]

# 2026-05-11 shingle-jaccard near-duplicate layer (conservative ≥0.95)
# Conservative near-duplicate cache layer.
# Catches reformatted prompts (whitespace, casing, punctuation) without
# returning wrong responses for genuinely different prompts.

import re as _re_sg

_SHINGLE_RING: list[tuple[frozenset, str, str, int]] = []  # (shingles, skeleton, response, expires)
_SHINGLE_RING_MAX = 200
_SHINGLE_THRESHOLD = 0.95


def _normalize(s: str) -> str:
    s = s.lower()
    s = _re_sg.sub(r"\s+", " ", s)
    s = _re_sg.sub(r"[^a-z0-9 ]+", "", s)
    return s.strip()


def _shingles(s: str, k: int = 3) -> frozenset:
    s = _normalize(s)
    if len(s) < k:
        return frozenset({s})
    return frozenset(s[i:i+k] for i in range(len(s) - k + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _skeleton(system: str, user: str, max_tokens: int) -> str:
    """Stable bucket key — must be identical for both prompts to be a candidate."""
    sys_h = (system or "")[:80].strip().lower()
    head = _normalize(user or "")[:40]
    bucket = (max_tokens // 200) * 200  # 200-token buckets
    return f"{sys_h}|{head}|{bucket}"


def near_get(system: str, user: str, max_tokens: int) -> str | None:
    """Return cached response for a near-duplicate prompt, or None."""
    sk = _skeleton(system, user, max_tokens)
    sh = _shingles(user)
    now = int(time.time())
    best_score = 0.0
    best_resp = None
    for shingles, skel, response, expires in _SHINGLE_RING:
        if expires <= now or skel != sk:
            continue
        score = _jaccard(sh, shingles)
        if score >= _SHINGLE_THRESHOLD and score > best_score:
            best_score = score
            best_resp = response
            if score == 1.0:
                break
    return best_resp


def near_set(system: str, user: str, max_tokens: int,
             response: str, ttl_sec: int = 3600) -> None:
    if not response or len(response) > 50_000:
        return
    sk = _skeleton(system, user, max_tokens)
    sh = _shingles(user)
    expires = int(time.time()) + ttl_sec
    _SHINGLE_RING.append((sh, sk, response, expires))
    if len(_SHINGLE_RING) > _SHINGLE_RING_MAX:
        # Drop oldest
        del _SHINGLE_RING[0]


def near_stats() -> dict:
    return {
        "shingle_ring_entries": len(_SHINGLE_RING),
        "shingle_ring_max": _SHINGLE_RING_MAX,
        "threshold": _SHINGLE_THRESHOLD,
    }

