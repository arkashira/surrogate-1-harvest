#!/usr/bin/env python3
"""axentx external-knowledge-ingester — pulls high-value external repos
(nexu-io/harness-engineering-guide, nexu-io/open-design) and ingests
their skills + patterns + design-systems into shared_knowledge so all
axentx agents can reference them before LLM calls.

User feedback 2026-05-04:
  > 'อะ + อันนี้อีกนิด https://github.com/nexu-io/harness-engineering-guide
  >  https://github.com/nexu-io/open-design'

Maps:
  harness-engineering-guide:
    skills/*.md          → shared_knowledge[nexu-skill/<name>]
    guide/*.md           → shared_knowledge[harness-pattern/<topic>]
    changelog/*.md       → shared_knowledge[harness-changelog/<date>]
  open-design:
    skills/*.md          → shared_knowledge[nexu-skill/<name>]
    design-systems/*     → shared_knowledge[design-system/<name>]
    docs/*.md            → shared_knowledge[open-design-doc/<topic>]

Cycle (every 6h, leader=GCP):
  1. git clone --depth 1 each repo to /tmp/external-kb/<repo>/
  2. Walk target subdirs, hash each .md file
  3. Skip if hash already in shared_kv["external-kb.ingested.<hash>"]
  4. Write to shared_knowledge with appropriate category
  5. Mark ingested

Audit:
  shared_kv["external-kb.last-pull"] = {ts, repos, files_ingested}
  shared_memory entries per major batch
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("EXT_KB_POLL_SEC", "21600"))   # 6 hours
HOST = socket.gethostname()
EXT_ROOT = Path(os.environ.get("EXT_KB_ROOT", "/tmp/external-kb"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

# Repo definitions: (clone_url, [(subdir, category, slug_prefix)])
EXTERNAL_REPOS = [
    ("https://github.com/nexu-io/harness-engineering-guide.git", [
        ("skills", "nexu-skill", "nexu-skill/harness"),
        ("guide", "harness-pattern", "harness-pattern"),
        ("changelog", "harness-changelog", "harness-changelog"),
    ]),
    ("https://github.com/nexu-io/open-design.git", [
        ("skills", "nexu-skill", "nexu-skill/open-design"),
        ("design-systems", "design-system", "design-system"),
        ("docs", "open-design-doc", "open-design-doc"),
    ]),
    # Firecrawl — 114k-star industry-leading web-scrape-to-markdown. Pull
    # their docs + examples so our scraper agents learn the patterns.
    ("https://github.com/firecrawl/firecrawl.git", [
        ("apps/api/src/scraper", "firecrawl-pattern", "firecrawl-pattern/scraper"),
        ("apps/api/sharedLibs", "firecrawl-pattern", "firecrawl-pattern/lib"),
    ]),
    # ── Tier 1 finds from 2026-05-04 GitHub deep-hunt ────────────────────
    # LiteLLM — 45.6k stars, unified LLM proxy + cost tracking + fallback
    ("https://github.com/BerriAI/litellm.git", [
        ("docs/my-website/docs/proxy", "litellm-pattern", "litellm-pattern/proxy"),
        ("docs/my-website/docs/observability", "litellm-pattern", "litellm-pattern/observability"),
        ("docs/my-website/docs/routing.md", "litellm-pattern", "litellm-pattern/routing"),
    ]),
    # Langfuse — 26.5k stars, OSS LLM observability (OTel-compatible)
    ("https://github.com/langfuse/langfuse.git", [
        ("README.md", "observability-pattern", "langfuse/readme"),
    ]),
    # DSPy — 34.2k stars, programmatic prompts + auto-optimization
    ("https://github.com/stanfordnlp/dspy.git", [
        ("docs/docs/learn", "dspy-pattern", "dspy-pattern/learn"),
        ("docs/docs/tutorials", "dspy-pattern", "dspy-pattern/tutorial"),
    ]),
    # Mem0 — 54.7k stars, universal agent memory layer
    ("https://github.com/mem0ai/mem0.git", [
        ("docs/core-concepts", "memory-pattern", "memory-pattern/mem0"),
        ("docs/quickstart.mdx", "memory-pattern", "memory-pattern/mem0-quickstart"),
    ]),
    # promptfoo — 20.8k stars, LLM eval CLI (used by OpenAI/Anthropic)
    ("https://github.com/promptfoo/promptfoo.git", [
        ("site/docs/configuration", "eval-pattern", "eval-pattern/promptfoo"),
    ]),
    # OpenLLMetry — 7.1k, OTel auto-instrumentation for LLMs
    ("https://github.com/traceloop/openllmetry.git", [
        ("packages/traceloop-sdk", "observability-pattern", "openllmetry-sdk"),
    ]),
    # GEPA — 4.2k, reflective prompt evolution (35× faster than RL)
    ("https://github.com/gepa-ai/gepa.git", [
        ("docs", "prompt-optimizer", "gepa/docs"),
        ("README.md", "prompt-optimizer", "gepa/readme"),
    ]),
]

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _sh(cmd: list[str], t: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def clone_or_pull(repo_url: str) -> Path | None:
    name = repo_url.rstrip(".git").rsplit("/", 1)[1]
    target = EXT_ROOT / name
    EXT_ROOT.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        # Update
        rc, _, _ = _sh(["git", "-C", str(target), "fetch", "origin"], t=60)
        if rc == 0:
            _sh(["git", "-C", str(target), "reset", "--hard",
                 "origin/main"], t=20)
        return target
    # Fresh clone
    rc, _, err = _sh(
        ["git", "clone", "--depth", "1", repo_url, str(target)], t=180)
    if rc != 0:
        log("ext-kb", f"  ✗ clone {name}: {err[:120]}")
        return None
    return target


def _is_ingested(file_hash: str) -> bool:
    try:
        from axentx_shared import kv_get
        return bool(kv_get(f"external-kb.ingested.{file_hash}"))
    except Exception:
        return False


def _mark_ingested(file_hash: str, slug: str) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(f"external-kb.ingested.{file_hash}", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "slug": slug,
        })
    except Exception:
        pass


def knowledge_set(slug: str, category: str, title: str,
                  body: str, metadata: dict) -> bool:
    if not (SB_URL and SB_KEY):
        return False
    try:
        payload = json.dumps({
            "p_slug": slug, "p_category": category,
            "p_title": title[:240], "p_body": body[:30000],
            "p_metadata": metadata,
            "p_who": "external-knowledge-ingester",
        }).encode()
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/rpc/shared_knowledge_set",
            data=payload, method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log("ext-kb", f"  ⚠ knowledge_set({slug}): {e}")
        return False


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return s[:60] or "x"


def ingest_dir(repo_root: Path, subdir: str, category: str,
               slug_prefix: str, repo_name: str) -> int:
    src = repo_root / subdir
    if not src.exists() or not src.is_dir():
        return 0
    written = 0
    for p in src.rglob("*.md"):
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(content) < 100:
            continue
        rel = p.relative_to(src)
        h = hashlib.md5(
            f"{repo_name}/{rel}/{len(content)}".encode()).hexdigest()[:14]
        if _is_ingested(h):
            continue
        # Title — use first H1 or filename
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = (title_match.group(1).strip() if title_match
                 else p.stem.replace("-", " ").title())[:200]
        slug_name = slugify(str(rel).replace("/", "-").replace(".md", ""))
        slug = f"{slug_prefix}/{slug_name}"
        if knowledge_set(slug, category, title, content, {
            "source_repo": repo_name,
            "source_path": str(rel),
            "ingested_at": datetime.datetime.utcnow().isoformat() + "Z",
        }):
            _mark_ingested(h, slug)
            written += 1
            if written >= 50:   # cap per dir per cycle
                break
    return written


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("ext-kb", "  ⤷ not leader — skip")
        return False

    total = 0
    per_repo = {}
    for repo_url, mappings in EXTERNAL_REPOS:
        name = repo_url.rstrip(".git").rsplit("/", 1)[1]
        log("ext-kb", f"▸ {name} — clone/pull")
        repo_root = clone_or_pull(repo_url)
        if not repo_root:
            continue
        repo_total = 0
        for subdir, category, slug_prefix in mappings:
            n = ingest_dir(repo_root, subdir, category, slug_prefix, name)
            repo_total += n
            log("ext-kb", f"  + {name}/{subdir} → {n} entries")
        per_repo[name] = repo_total
        total += repo_total

    log("ext-kb", f"  ✓ ingested {total} entries from "
                  f"{len(EXTERNAL_REPOS)} external repos")

    try:
        from axentx_shared import kv_set, memory_log
        kv_set("external-kb.last-pull", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
            "repos": list(per_repo.keys()),
            "per_repo": per_repo,
            "total": total,
        })
        memory_log("ext-kb", "kb-ingested",
                   f"ingested {total} entries from external repos",
                   body=json.dumps(per_repo, indent=2),
                   tags=["external-kb", HOST])
    except Exception:
        pass
    return False


if __name__ == "__main__":
    daemon_loop("ext-kb", POLL_SEC, cycle)
