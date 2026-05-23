#!/usr/bin/env python3
"""axentx github-ideabank — clones curated GitHub idea repositories,
parses their structured idea entries (each repo has its own format), and
emits to validator-queue.

These repos are pre-curated by humans/AI and have STRUCTURED schemas:
  - florinpop17/app-ideas — 327⭐ — beginner/intermediate/advanced project ideas
  - idea-box (multiple) — pain points with TAM/WTP/incumbents/gap fields
  - Micro-SaaS-Examples/Best-Micro-SaaS-Tools — niche micro-SaaS examples
  - merklefruit/SaaS4Devs — bootstrap + validation playbooks

Each repo gets cloned weekly to /opt/surrogate-1-harvest/data/ideabank/<slug>
and parsed. New ideas → validator-queue.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,  # noqa: E402
                             new_trace_id)

POLL_SEC = int(os.environ.get("IDEABANK_POLL_SEC", "86400"))   # daily
DATA_DIR = REPO_ROOT / "data" / "ideabank"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = REPO_ROOT / "state" / "github-ideabank.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)

# Repos to mine. Format: (slug, git_url, type)
# type: "markdown" = walk all .md files, parse top-level headings as ideas
SOURCES = [
    ("app-ideas",            "https://github.com/florinpop17/app-ideas", "markdown"),
    ("Best-Micro-SaaS-Tools", "https://github.com/Micro-SaaS-Examples/Best-Micro-SaaS-Tools", "markdown"),
    ("SaaS4Devs",            "https://github.com/nicolas-racchi/SaaS4Devs", "markdown"),
    ("awesome-saas-startups", "https://github.com/zupcode-com/awesome-free-services-for-your-next-startup-or-saas", "markdown"),
]

EXTRACT_SYSTEM = (
    "You are a startup analyst extracting product ideas from a curated "
    "GitHub idea collection. Each entry is a candidate startup. Decide if "
    "it has a credible PAID monetization path (B2B SaaS, not free OSS) "
    "and if axentx should spawn an adjacent variant."
)

EXTRACT_PROMPT = """Idea entry from {repo}:

{entry}

Output STRICT JSON:
{{
  "title": "the idea title",
  "problem": "1-sentence concrete pain — be specific",
  "audience": "who pays",
  "monetization": "subscription|usage|enterprise|marketplace|none",
  "monetization_signal": "low|medium|high",
  "pricing_guess": "$X-Y/seat/mo or 'free' if no path to revenue",
  "axentx_idea": "1-sentence axentx-flavored take, or null if not promising",
  "tam_signal": "low|medium|high",
  "skip_reason": "if not promising: 1 sentence; else null"
}}
"""


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def load_seen() -> set:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(s: set) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(s)[-10000:]))
    except Exception:
        pass


def clone_or_pull(slug: str, url: str) -> Path:
    target = DATA_DIR / slug
    if target.exists() and (target / ".git").exists():
        try:
            subprocess.run(
                ["git", "-C", str(target), "fetch", "--depth", "1", "origin"],
                capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(target), "reset", "--hard", "origin/HEAD"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception as e:
            log("github-ideabank",
                f"  ⚠ pull {slug}: {type(e).__name__}: {str(e)[:80]}")
    else:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(target)],
                capture_output=True, text=True, timeout=120, check=True,
            )
        except subprocess.CalledProcessError as e:
            err = e.stderr if isinstance(e.stderr, str) else (
                e.stderr.decode("utf-8", "replace") if e.stderr else "?")
            log("github-ideabank", f"  ✗ clone {slug}: {err[:160]}")
            return target
    return target


def parse_markdown_repo(repo_dir: Path) -> list[dict]:
    """Walk all .md files. Each H2/H3 = one idea entry."""
    entries = []
    for md_file in repo_dir.rglob("*.md"):
        # Skip standard docs
        if md_file.name.lower() in (
                "license.md", "code_of_conduct.md", "contributing.md"):
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Find h2/h3 sections
        sections = re.split(r"\n##+ ", text)
        for sec in sections[1:]:   # skip pre-first-heading content
            lines = sec.split("\n", 1)
            title = lines[0].strip("# \t")
            body = lines[1] if len(lines) > 1 else ""
            # Trim body to next h2/h3 (already split, this is just safety)
            body = body[:3000].strip()
            if len(title) < 5 or len(body) < 80:
                continue
            entries.append({
                "title": title,
                "body": body,
                "file": str(md_file.relative_to(repo_dir)),
            })
    return entries


def extract_signals(repo_slug: str, entry: dict) -> dict | None:
    full = (
        f"Title: {entry['title']}\n"
        f"File: {entry.get('file','?')}\n\n"
        f"Body:\n{entry['body'][:2500]}"
    )
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(repo=repo_slug, entry=full),
            system=EXTRACT_SYSTEM, max_tokens=400, timeout=30,
        )
    except Exception:
        return None
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```", 2)
        if len(seg) >= 2:
            txt = seg[1]
            if txt.startswith("json"):
                txt = txt[4:]
            txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        return None


def emit(repo_slug: str, entry: dict, signals: dict) -> None:
    h = hashlib.sha1((repo_slug + entry["title"]).encode()).hexdigest()[:14]
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"gh-idea-{h}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": f"github-ideabank/{repo_slug}",
        "url": f"https://github.com/{repo_slug}",
        "title": entry["title"],
        "pain_one_liner": signals.get("problem", "")[:240],
        "audience": signals.get("audience", ""),
        "monetization": signals.get("monetization", ""),
        "monetization_signal": signals.get("monetization_signal", "low"),
        "pricing_signal": signals.get("pricing_guess", ""),
        "tam_signal": signals.get("tam_signal", "low"),
        "axentx_idea": signals.get("axentx_idea") or "",
        "raw_signals": signals,
        "authority_score": 0.6,   # community-curated, not VC-grade
        "history": [{
            "stage": "research",
            "actor": "github-ideabank",
            "output": (f"gh-ideabank/{repo_slug}: {entry['title'][:60]} | "
                       f"mon={signals.get('monetization_signal','?')}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    total_emitted = 0
    for slug, url, kind in SOURCES:
        if _stop:
            break
        repo_dir = clone_or_pull(slug, url)
        if not repo_dir.exists():
            continue
        entries = parse_markdown_repo(repo_dir) if kind == "markdown" else []
        log("github-ideabank",
            f"  {slug}: parsed {len(entries)} candidate ideas")

        for e in entries[:50]:   # cap per repo per cycle
            if _stop:
                break
            h = hashlib.sha1((slug + e["title"]).encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            signals = extract_signals(slug, e)
            if not signals:
                continue
            mon = (signals.get("monetization_signal") or "").lower()
            if mon not in ("medium", "high"):
                continue   # skip free-OSS-only ideas
            emit(slug, e, signals)
            total_emitted += 1
            time.sleep(1)
    save_seen(seen)
    log("github-ideabank", f"cycle: emitted {total_emitted}")
    return total_emitted > 0


if __name__ == "__main__":
    daemon_loop("github-ideabank", POLL_SEC, do_one)
