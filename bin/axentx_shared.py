"""axentx shared-context library — read/write the cross-host KV/memory/knowledge.

Used by every daemon that needs to consult or update state shared across
GCP / Kam1 / Kam2 / future hosts. Wraps the shared_kv / shared_memory /
shared_knowledge tables defined in db/shared-context-schema.sql.

Design:
  - All ops go through Supabase REST (no DB driver dep).
  - Reads use anon key (cheap, no service-role exposure).
  - Writes use service-role key (RLS allows).
  - Best-effort: never raises on network failure; returns sensible defaults.

Usage from a daemon:

    from axentx_shared import (kv_get, kv_set, memory_log,
                               knowledge_get, knowledge_search)

    # Read operator persona
    persona = kv_get("operator.persona") or {"name": "axentx-default"}

    # Log a lesson (host name auto-detected)
    memory_log("dev", "lesson", "claude/llm-fallback-chain",
               body="DeepSeek R1 timed out on long prompts; fall through "
                    "to V3 by default", tags=["llm", "deepseek"])

    # Search knowledge
    hits = knowledge_search("circuit breaker", limit=3)
    for h in hits:
        prompt += f"\n## {h['title']}\n{h['body']}"
"""
from __future__ import annotations
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

_SB_URL = os.environ.get("SUPABASE_URL", "")
_SB_ANON = os.environ.get("SUPABASE_ANON_KEY", "")
_SB_SECRET = (os.environ.get("SUPABASE_SECRET_KEY")
              or os.environ.get("SUPABASE_SERVICE_KEY", ""))

# 2026-05-08 D1 migration
# CF Worker (D1-backed) primary layer for kv/memory/knowledge.
# When Supabase is degraded (timeouts, 522s), CF responds in 50-600ms reliably.
_CF_URL = os.environ.get("CF_WORKER_URL", "https://surrogate-1-cursor.ashira.workers.dev").rstrip("/")
_CF_TIMEOUT = int(os.environ.get("CF_TIMEOUT_SEC", "5"))


def _cf_req(method, path, body=None, qs=None):
    """Hit CF Worker. Returns parsed JSON on success, None on failure."""
    full = f"{_CF_URL}{path}"
    if qs:
        full += "?" + urllib.parse.urlencode(qs)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        full, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "axentx"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_CF_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # not-found is a normal signal, not error
        return None
    except Exception:
        return None

_HOST_NAME = (os.environ.get("XVM_HOST")
              or os.environ.get("AXENTX_HOST")
              or socket.gethostname())


def _req(method: str, path: str, body: dict | None = None,
         service: bool = False, timeout: int = 8) -> dict | list | None:
    if not _SB_URL:
        return None
    key = _SB_SECRET if service else (_SB_ANON or _SB_SECRET)
    if not key:
        return None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_SB_URL}{path}", data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except (urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, json.JSONDecodeError):
        return None



# ── FS cache layer (writes-through, reads-first) ─────────────────────────
# 2026-05-05: Supabase 522 from Kam2 was blocking demand-amplifier signals.
# Each kv_get call timed out at 8s × 200 daemons → catastrophic blocking.
# Local FS cache gives immediate reads even when Supabase is unreachable.
import hashlib as _hashlib
import os as _os
import time as _time

_KV_FS_DIR = _os.environ.get(
    "KV_FS_CACHE_DIR",
    "/opt/surrogate-1-harvest/state/kv-cache")
_KV_FS_TTL = int(_os.environ.get("KV_FS_TTL_SEC", "300"))   # 5 min default

def _kv_fs_cache_dir():
    _os.makedirs(_KV_FS_DIR, exist_ok=True)
    return _KV_FS_DIR

def _kv_fs_path(key: str) -> str:
    h = _hashlib.md5(key.encode()).hexdigest()[:24]
    return _os.path.join(_kv_fs_cache_dir(), f"{h}.json")

def _kv_fs_read(key: str, max_age_sec: int = _KV_FS_TTL):
    """Read from FS cache. Returns (value, age_sec) or (None, None) on miss/expired."""
    path = _kv_fs_path(key)
    try:
        mtime = _os.path.getmtime(path)
        age = _time.time() - mtime
        if age > max_age_sec:
            return None, None
        with open(path) as f:
            return json.load(f), age
    except (FileNotFoundError, ValueError, OSError):
        return None, None

def _kv_fs_write(key: str, value) -> None:
    """Atomic write to FS cache."""
    path = _kv_fs_path(key)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(value, f)
        _os.replace(tmp, path)
    except OSError:
        pass

# ── KV ──────────────────────────────────────────────────────────────────
def kv_get(key: str) -> dict | list | str | int | None:
    """Read a JSONB value by key.
    Layers: FS cache (5min) -> CF Worker D1 -> Supabase -> stale FS.
    2026-05-08: CF added as primary cross-VM layer; Supabase degraded.
    """
    # Layer 1: fresh FS hit
    val, age = _kv_fs_read(key)
    if val is not None:
        return val

    # Layer 2: CF Worker (D1-backed)
    cf = _cf_req("GET", f"/kv/{urllib.parse.quote(key, safe='')}")
    if isinstance(cf, dict) and "v" in cf:
        v = cf.get("v")
        _kv_fs_write(key, v)
        return v

    # Layer 3: Supabase (legacy fallback)
    qs = urllib.parse.urlencode({"k": f"eq.{key}", "select": "v", "limit": 1})
    r = _req("GET", f"/rest/v1/shared_kv?{qs}", timeout=2)
    if isinstance(r, list) and r:
        v = r[0].get("v")
        _kv_fs_write(key, v)
        return v

    # Layer 4: stale FS fallback (better than None for demand signals)
    stale, _ = _kv_fs_read(key, max_age_sec=86400)
    return stale


def kv_set(key: str, value) -> bool:
    """Write FS + CF (D1) + best-effort Supabase. 2026-05-08: CF layer added."""
    _kv_fs_write(key, value)
    # CF Worker (primary cross-VM layer)
    _cf_req("POST", f"/kv/{urllib.parse.quote(key, safe='')}",
            {"value": value, "who": _HOST_NAME})
    # Best-effort Supabase (legacy)
    r = _req("POST", "/rest/v1/rpc/shared_kv_set",
             {"p_k": key, "p_v": value, "p_who": _HOST_NAME},
             service=True, timeout=2)
    return True


# ── Memory (append-only) ─────────────────────────────────────────────────
def memory_log(actor: str, kind: str, title: str, body: str = "",
               tags: list[str] | None = None,
               payload: dict | None = None) -> bool:
    """Append a memory row. CF first, Supabase as best-effort.
    Caller picks kind from {lesson, fix, pref, event}."""
    row = {
        "host": _HOST_NAME,
        "actor": actor,
        "kind": kind,
        "title": title[:240],
        "body": body[:8000] if body else None,
        "tags": tags or [],
        "payload": payload,
    }
    # CF first
    cf = _cf_req("POST", "/memory/log", row)
    cf_ok = isinstance(cf, dict) and cf.get("ok")
    # Best-effort Supabase
    _req("POST", "/rest/v1/shared_memory", row, service=True)
    return cf_ok


def memory_recent(kind: str | None = None, limit: int = 20) -> list[dict]:
    """Read recent memory entries. CF first, Supabase fallback."""
    qs = {"limit": str(limit)}
    if kind:
        qs["kind"] = kind
    cf = _cf_req("GET", "/memory/recent", qs=qs)
    if isinstance(cf, list):
        return cf
    # Supabase fallback
    params = {"select": "*", "order": "created_at.desc", "limit": str(limit)}
    if kind:
        params["kind"] = f"eq.{kind}"
    qs2 = urllib.parse.urlencode(params)
    r = _req("GET", f"/rest/v1/shared_memory?{qs2}")
    return r if isinstance(r, list) else []


# ── Knowledge ────────────────────────────────────────────────────────────
def knowledge_get(slug: str) -> dict | None:
    """Fetch a knowledge entry. CF first, Supabase fallback."""
    cf = _cf_req("GET", "/knowledge/get", qs={"key": slug})
    if isinstance(cf, dict) and cf.get("k"):
        # Normalize CF shape -> Supabase-compatible shape
        return {"slug": cf.get("k"), "body": cf.get("content", ""),
                "metadata": cf.get("tags"), "updated_at": cf.get("ts"),
                "category": "", "title": ""}
    # Supabase fallback
    qs = urllib.parse.urlencode({
        "slug": f"eq.{slug}",
        "select": "slug,category,title,body,metadata,updated_at",
        "limit": 1,
    })
    r = _req("GET", f"/rest/v1/shared_knowledge?{qs}")
    if isinstance(r, list) and r:
        return r[0]
    return None


def knowledge_set(slug: str, category: str, title: str, body: str,
                  metadata: dict | None = None) -> bool:
    """Upsert a knowledge entry. CF first + Supabase best-effort."""
    # CF: combine title+body (D1 schema is simpler — k+content+tags)
    content = f"{title}\n\n{body}" if title else body
    tags = []
    if category:
        tags.append(f"category:{category}")
    if metadata:
        tags.append(f"meta:{json.dumps(metadata)[:500]}")
    cf = _cf_req("POST", "/knowledge/set",
                 {"key": slug, "content": content, "tags": tags})
    cf_ok = isinstance(cf, dict) and cf.get("ok")
    # Supabase best-effort
    _req("POST", "/rest/v1/rpc/shared_knowledge_set", {
        "p_slug": slug, "p_category": category,
        "p_title": title, "p_body": body,
        "p_metadata": metadata or {}, "p_who": _HOST_NAME,
    }, service=True)
    return cf_ok or False


def knowledge_search(query: str, category: str | None = None,
                     limit: int = 5) -> list[dict]:
    """Free-text search. CF /knowledge/search (LIKE) first, Supabase fallback."""
    cf = _cf_req("GET", "/knowledge/search",
                 qs={"q": query, "limit": str(limit)})
    if isinstance(cf, list) and cf:
        # Normalize to old shape
        out = []
        for row in cf:
            out.append({
                "slug": row.get("k"), "body": row.get("content", ""),
                "title": "", "category": category or "", "metadata": row.get("tags"),
            })
        return out
    # Supabase fallback
    pattern = f"*{query}*"
    params = {
        "or": f"(title.ilike.{pattern},body.ilike.{pattern})",
        "select": "slug,category,title,body,metadata",
        "order": "updated_at.desc",
        "limit": str(limit),
    }
    if category:
        params["category"] = f"eq.{category}"
    qs = urllib.parse.urlencode(params)
    r = _req("GET", f"/rest/v1/shared_knowledge?{qs}")
    return r if isinstance(r, list) else []


__all__ = [
    "kv_get", "kv_set",
    "memory_log", "memory_recent",
    "knowledge_get", "knowledge_set", "knowledge_search",
]
