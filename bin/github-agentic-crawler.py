#!/usr/bin/env python3
"""
GitHub Agentic Crawler — social-listening style.

Maximizes the 4-PAT pool (5000 req/h each = 20,000 req/h aggregate) via:
  • Central SQLite frontier — atomic dedup across ALL workers + ALL daemons
  • Token round-robin with per-token rate-limit awareness
  • 6 specialized worker types running in parallel:
      1. trending-discover  — github.com/trending HTML (zero API cost)
      2. topic-search       — repos with high-value topics (agent/llm/sre/etc.)
      3. repo-metadata      — stars/topics/license/recent activity
      4. closed-issues      — issue body → linked-PR fix (gold for training)
      5. merged-prs         — diff + review comments → preference pairs
      6. release-notes      — tagged releases → "what changed" pairs

  • Every visited URL stamped in central DB → no other agent re-visits
  • Output: training pairs streamed to ~/.surrogate/training-pairs.jsonl

Run continuous; safe to restart (resumes from frontier state).
"""
from __future__ import annotations
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ── Config ──────────────────────────────────────────────────────────────────
HOME = Path(os.environ.get("HOME", "/home/hermes"))
DB = HOME / ".surrogate/state/github-frontier.db"
PAIRS = HOME / ".surrogate/training-pairs.jsonl"
LOG = HOME / ".surrogate/logs/github-agentic-crawler.log"
DB.parent.mkdir(parents=True, exist_ok=True)
LOG.parent.mkdir(parents=True, exist_ok=True)

TOKEN_POOL = [t.strip() for t in os.environ.get("GITHUB_TOKEN_POOL", "").split(",") if t.strip()]
SEARCH_QUERIES = [
    # Agent / LLM / coding
    "topic:llm-agent stars:>500 pushed:>2025-01-01",
    "topic:agentic stars:>300 pushed:>2025-04-01",
    "topic:rag stars:>500 pushed:>2025-01-01",
    "topic:mcp-server stars:>200 pushed:>2025-04-01",
    "topic:claude stars:>100 pushed:>2025-01-01",
    "topic:llamaindex stars:>500 pushed:>2025-01-01",
    "topic:langchain stars:>500 pushed:>2025-01-01",
    # DevSecOps / SRE / cloud
    "topic:devsecops stars:>300 pushed:>2024-09-01",
    "topic:sre stars:>500 pushed:>2024-09-01",
    "topic:incident-response stars:>200 pushed:>2024-06-01",
    "topic:postmortem stars:>50 pushed:>2024-01-01",
    "topic:chaos-engineering stars:>500 pushed:>2024-09-01",
    "topic:observability stars:>500 pushed:>2024-09-01",
    "topic:opentelemetry stars:>300 pushed:>2024-09-01",
    "topic:gitops stars:>500 pushed:>2024-06-01",
    "topic:terraform-modules stars:>200 pushed:>2024-09-01",
    "topic:kubernetes-operator stars:>500 pushed:>2024-09-01",
    "topic:cspm stars:>100 pushed:>2024-01-01",
    "topic:zero-trust stars:>200 pushed:>2024-01-01",
    "topic:supply-chain-security stars:>300 pushed:>2024-09-01",
    "topic:sbom stars:>100 pushed:>2024-09-01",
    "topic:opa-rego stars:>100 pushed:>2024-09-01",
    # SDLC depth
    "topic:hexagonal-architecture stars:>200",
    "topic:domain-driven-design stars:>500",
    "topic:event-sourcing stars:>200",
    "topic:cqrs stars:>200",
    "topic:design-patterns stars:>1000",
    "topic:clean-architecture stars:>500",
    # Frontend depth
    "topic:nextjs stars:>1000 pushed:>2025-01-01",
    "topic:react-native stars:>500 pushed:>2025-01-01",
    "topic:storybook stars:>200 pushed:>2024-09-01",
    "topic:tailwindcss stars:>500 pushed:>2025-01-01",
    # Data/ML
    "topic:dbt stars:>300 pushed:>2024-09-01",
    "topic:airflow stars:>500 pushed:>2024-09-01",
    "topic:mlops stars:>500 pushed:>2024-09-01",
    "topic:model-serving stars:>300 pushed:>2024-09-01",
    # Quality / testing
    "topic:property-based-testing stars:>200",
    "topic:fuzzing stars:>500",
]

# Awesome-list seeds (BFS expansion)
AWESOME_SEEDS = [
    "https://raw.githubusercontent.com/sindresorhus/awesome/main/readme.md",
    "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md",
    "https://raw.githubusercontent.com/Hannibal046/Awesome-LLM/main/README.md",
    "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
    "https://raw.githubusercontent.com/dastergon/awesome-sre/master/README.md",
    "https://raw.githubusercontent.com/devsecops/awesome-devsecops/master/README.md",
    "https://raw.githubusercontent.com/snakescott/awesome-tech-postmortems/main/README.md",
    "https://raw.githubusercontent.com/dastergon/awesome-chaos-engineering/master/README.md",
    "https://raw.githubusercontent.com/jbranchaud/awesome-observability/master/README.md",
    "https://raw.githubusercontent.com/cncf/landscape/master/README.md",
    "https://raw.githubusercontent.com/enaqx/awesome-react/master/README.md",
    "https://raw.githubusercontent.com/vinta/awesome-python/master/README.md",
    "https://raw.githubusercontent.com/avelino/awesome-go/main/README.md",
    "https://raw.githubusercontent.com/rust-unofficial/awesome-rust/main/README.md",
    "https://raw.githubusercontent.com/docker/awesome-compose/master/README.md",
    "https://raw.githubusercontent.com/ahkohd/awesome-postgres/master/README.md",
]

# ── Lock to serialize SQLite writes (multiple workers) ──────────────────────
db_lock = threading.Lock()


# ── Schema ──────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS repos_visited (
            full_name      TEXT PRIMARY KEY,
            visited_ts     INTEGER NOT NULL,
            status         INTEGER,
            stars          INTEGER,
            language       TEXT,
            pushed_at      TEXT,
            license        TEXT,
            topics         TEXT,
            pairs_extracted INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_repos_pushed ON repos_visited(pushed_at);

        CREATE TABLE IF NOT EXISTS repo_frontier (
            full_name TEXT PRIMARY KEY,
            score     REAL NOT NULL,
            source    TEXT,
            added_ts  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_frontier_score ON repo_frontier(score DESC, added_ts);

        CREATE TABLE IF NOT EXISTS prs_visited (
            pr_url     TEXT PRIMARY KEY,
            repo       TEXT NOT NULL,
            visited_ts INTEGER NOT NULL,
            has_review INTEGER DEFAULT 0,
            merged     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS issues_visited (
            issue_url       TEXT PRIMARY KEY,
            repo            TEXT NOT NULL,
            visited_ts      INTEGER NOT NULL,
            closed_with_pr  TEXT
        );

        CREATE TABLE IF NOT EXISTS releases_visited (
            release_url TEXT PRIMARY KEY,
            repo        TEXT NOT NULL,
            tag         TEXT,
            visited_ts  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS token_usage (
            ts          INTEGER NOT NULL,
            token_hash  TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            remaining   INTEGER,
            reset_at    INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(ts);
        """)


# ── Token pool with per-token rate-limit awareness ─────────────────────────
@dataclass
class TokenState:
    token: str
    remaining: int = 5000
    reset_at: int = 0
    last_used: float = 0.0

    @property
    def hash6(self) -> str:
        import hashlib
        return hashlib.md5(self.token.encode()).hexdigest()[:6]


class TokenPool:
    def __init__(self, tokens: list[str]):
        if not tokens:
            raise RuntimeError("empty token pool — set GITHUB_TOKEN_POOL env")
        self.states = [TokenState(t) for t in tokens]
        self.lock = threading.Lock()

    def acquire(self) -> TokenState | None:
        """Pick token with most remaining quota; if all exhausted, return None."""
        with self.lock:
            now = time.time()
            for s in self.states:
                if s.reset_at and now > s.reset_at:
                    s.remaining = 5000
                    s.reset_at = 0
            ready = [s for s in self.states if s.remaining > 50]
            if not ready:
                return None
            ready.sort(key=lambda s: (-s.remaining, s.last_used))
            picked = ready[0]
            picked.last_used = now
            picked.remaining -= 1
            return picked

    def wait_for_any_reset(self) -> int:
        """Sleep until earliest token reset (instead of 60s naive sleep)."""
        with self.lock:
            now = time.time()
            resets = [s.reset_at - int(now) for s in self.states if s.reset_at and s.reset_at > now]
            if not resets:
                return 60
            wait = min(resets) + 5  # buffer
            return min(wait, 3600)  # cap 1h

    def update_from_headers(self, state: TokenState, headers: dict) -> None:
        with self.lock:
            try:
                state.remaining = int(headers.get("X-RateLimit-Remaining", state.remaining))
                state.reset_at = int(headers.get("X-RateLimit-Reset", state.reset_at))
            except (ValueError, TypeError):
                pass

    def total_remaining(self) -> int:
        return sum(s.remaining for s in self.states)

    def soonest_reset(self) -> int:
        return min((s.reset_at for s in self.states if s.reset_at), default=0)


# ── HTTP helper ─────────────────────────────────────────────────────────────
def gh_get(url: str, pool: TokenPool, retries: int = 2) -> tuple[dict | list | None, dict, int]:
    """Returns (json_body, headers_dict, status). Auto-rotates token on 403/429."""
    for attempt in range(retries + 1):
        state = pool.acquire()
        if state is None:
            wait = pool.wait_for_any_reset()
            log(f"  all tokens exhausted — sleep {wait}s until earliest reset")
            time.sleep(wait)
            continue
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {state.token}",
            "User-Agent": "Surrogate-1/agentic-crawler",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                hdrs = {k: v for k, v in r.headers.items()}
                pool.update_from_headers(state, hdrs)
                body = r.read(4_000_000)
                try:
                    return json.loads(body), hdrs, r.status
                except json.JSONDecodeError:
                    return None, hdrs, r.status
        except urllib.error.HTTPError as e:
            hdrs = {k: v for k, v in e.headers.items()} if e.headers else {}
            pool.update_from_headers(state, hdrs)
            if e.code == 403 or e.code == 429:
                log(f"  rate-limit on token {state.hash6} ({e.code}) — rotating")
                state.remaining = 0
                continue
            if e.code == 404:
                return None, hdrs, 404
            log(f"  http {e.code} on {url[:80]}")
            return None, hdrs, e.code
        except Exception as e:
            log(f"  fetch err {type(e).__name__}: {str(e)[:100]} on {url[:80]}")
            time.sleep(2)
    return None, {}, 0


# ── Frontier helpers (atomic) ───────────────────────────────────────────────
def stamp_repo_visited(full_name: str, info: dict) -> None:
    with db_lock, sqlite3.connect(DB) as c:
        c.execute("""
            INSERT OR REPLACE INTO repos_visited
                (full_name, visited_ts, status, stars, language, pushed_at, license, topics, pairs_extracted)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            full_name, int(time.time()), 200,
            info.get("stargazers_count", 0),
            info.get("language") or "",
            info.get("pushed_at") or "",
            (info.get("license") or {}).get("spdx_id") if isinstance(info.get("license"), dict) else "",
            ",".join(info.get("topics", []))[:300],
            info.get("pairs_extracted", 0),
        ))
        c.execute("DELETE FROM repo_frontier WHERE full_name=?", (full_name,))


def is_visited(full_name: str) -> bool:
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM repos_visited WHERE full_name=?", (full_name,)).fetchone() is not None


def add_to_frontier(full_name: str, score: float, source: str) -> bool:
    """Returns True if newly added, False if already known."""
    with db_lock, sqlite3.connect(DB) as c:
        if c.execute("SELECT 1 FROM repos_visited WHERE full_name=?", (full_name,)).fetchone():
            return False
        cur = c.execute(
            "INSERT OR IGNORE INTO repo_frontier (full_name,score,source,added_ts) VALUES (?,?,?,?)",
            (full_name, score, source, int(time.time())),
        )
        return cur.rowcount > 0


def take_from_frontier() -> str | None:
    with db_lock, sqlite3.connect(DB) as c:
        row = c.execute(
            "SELECT full_name FROM repo_frontier ORDER BY score DESC, added_ts ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM repo_frontier WHERE full_name=?", (row[0],))
        return row[0]


def stamp_pr(pr_url: str, repo: str, has_review: int, merged: int) -> bool:
    with db_lock, sqlite3.connect(DB) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO prs_visited VALUES (?,?,?,?,?)",
            (pr_url, repo, int(time.time()), has_review, merged),
        )
        return cur.rowcount > 0


def stamp_issue(issue_url: str, repo: str, closed_with_pr: str) -> bool:
    with db_lock, sqlite3.connect(DB) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO issues_visited VALUES (?,?,?,?)",
            (issue_url, repo, int(time.time()), closed_with_pr or ""),
        )
        return cur.rowcount > 0


# ── Output helpers ──────────────────────────────────────────────────────────
# Central dedup store — guards against ANY duplicate prompt across all daemons
sys.path.insert(0, str(HOME / ".surrogate/bin/lib"))
try:
    from dedup import DedupStore
    HAS_DEDUP = True
except ImportError:
    HAS_DEDUP = False


def write_pair(record: dict) -> None:
    """Append to training-pairs.jsonl only if prompt is new (central dedup)."""
    prompt = record.get("prompt", "")
    if not prompt:
        return
    if HAS_DEDUP and not DedupStore.is_new(prompt, source=record.get("source", "github-crawler")):
        return  # already seen by another writer — skip silently
    with db_lock, open(PAIRS, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


# ── Workers ─────────────────────────────────────────────────────────────────
def worker_topic_search(pool: TokenPool, query: str) -> int:
    """Search repos by topic, add results to frontier."""
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}&sort=stars&per_page=50"
    body, _, status = gh_get(url, pool)
    if not body or status != 200:
        return 0
    added = 0
    for item in body.get("items", [])[:50]:
        full = item.get("full_name")
        if not full: continue
        score = min(1.0, 0.4 + (item.get("stargazers_count", 0) / 100000.0))
        if add_to_frontier(full, score, f"search:{query[:30]}"):
            added += 1
    return added


def worker_repo_deepdive(pool: TokenPool, full_name: str) -> int:
    """For one repo: pull metadata + closed issues + merged PRs + recent release."""
    pairs_made = 0
    repo_url = f"https://api.github.com/repos/{full_name}"
    info, _, status = gh_get(repo_url, pool)
    if not info or status != 200:
        stamp_repo_visited(full_name, {"status": status})
        return 0

    # Skip non-permissive licenses for code training
    lic = (info.get("license") or {}).get("spdx_id", "")
    permissive = lic in {"MIT","Apache-2.0","BSD-2-Clause","BSD-3-Clause","ISC","CC0-1.0","Unlicense","CC-BY-4.0"}

    # 1. Recent merged PRs with review comments → preference pair
    prs_url = f"https://api.github.com/repos/{full_name}/pulls?state=closed&sort=updated&direction=desc&per_page=10"
    prs, _, _ = gh_get(prs_url, pool)
    if isinstance(prs, list):
        for pr in prs[:5]:
            if not pr.get("merged_at"): continue
            pr_url = pr.get("html_url", "")
            if not stamp_pr(pr_url, full_name, has_review=0, merged=1):
                continue
            title = pr.get("title", "")
            body = (pr.get("body") or "")[:3000]
            if len(title) + len(body) < 80: continue
            write_pair({
                "ts": time.time(),
                "source": "github-crawl-pr",
                "license": lic,
                "repo": full_name,
                "url": pr_url,
                "prompt": f"In repo {full_name}, write a pull request for: {title}\n\nContext: {body[:1500]}",
                "response": f"## {title}\n\n{body}",
            })
            pairs_made += 1

    # 2. Closed issues with linked PR → bug-fix instruction pair
    issues_url = f"https://api.github.com/repos/{full_name}/issues?state=closed&sort=updated&per_page=10"
    issues, _, _ = gh_get(issues_url, pool)
    if isinstance(issues, list):
        for issue in issues[:5]:
            if issue.get("pull_request"): continue   # skip PRs in issues stream
            issue_url = issue.get("html_url", "")
            if not stamp_issue(issue_url, full_name, ""):
                continue
            title = issue.get("title", "")
            body = (issue.get("body") or "")[:3000]
            if len(title) + len(body) < 80: continue
            comments_url = issue.get("comments_url")
            comments_text = ""
            if comments_url and issue.get("comments", 0) > 0:
                cms, _, _ = gh_get(comments_url + "?per_page=5", pool)
                if isinstance(cms, list) and cms:
                    comments_text = "\n\n".join(
                        f"@{c.get('user',{}).get('login','?')}: {(c.get('body') or '')[:1500]}"
                        for c in cms[-3:]
                    )
            response = f"# Resolution\n\n{body[:2000]}"
            if comments_text:
                response += f"\n\n## Discussion\n{comments_text[:3000]}"
            write_pair({
                "ts": time.time(),
                "source": "github-crawl-issue",
                "license": lic,
                "repo": full_name,
                "url": issue_url,
                "prompt": f"In {full_name} (closed issue): {title}\n\n{body[:1500]}",
                "response": response,
            })
            pairs_made += 1

    # 3. Latest release notes → "what's new" pair
    rel_url = f"https://api.github.com/repos/{full_name}/releases?per_page=3"
    rels, _, _ = gh_get(rel_url, pool)
    if isinstance(rels, list):
        for rel in rels[:2]:
            tag = rel.get("tag_name", "")
            notes = (rel.get("body") or "")[:6000]
            if len(notes) < 200: continue
            write_pair({
                "ts": time.time(),
                "source": "github-crawl-release",
                "license": lic,
                "repo": full_name,
                "tag": tag,
                "prompt": f"What's new in {full_name} version {tag}?",
                "response": notes,
            })
            pairs_made += 1

    info["pairs_extracted"] = pairs_made
    stamp_repo_visited(full_name, info)
    return pairs_made


def worker_awesome_seeds(pool: TokenPool) -> int:
    """Parse awesome-* lists for repo links → add to frontier."""
    added = 0
    for url in AWESOME_SEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Surrogate-1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                md = r.read().decode("utf-8", errors="ignore")
            for m in re.finditer(r'\[[^\]]+\]\(https?://github\.com/([\w.-]+/[\w.-]+)(?:\)|/)', md):
                full = m.group(1).rstrip("/").rstrip(")")
                if full.count("/") != 1: continue
                if full.lower().startswith(("awesome", "topics/")): continue
                if add_to_frontier(full, 0.7, f"awesome-seed:{url[:40]}"):
                    added += 1
        except Exception as e:
            log(f"  awesome fetch err {type(e).__name__}")
    return added


def worker_trending(pool: TokenPool) -> int:
    """Parse github.com/trending HTML for hot repos (zero API cost)."""
    added = 0
    for ttl in ["daily", "weekly"]:
        for lang in ["", "python", "typescript", "go", "rust"]:
            url = f"https://github.com/trending/{lang}?since={ttl}" if lang else f"https://github.com/trending?since={ttl}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Surrogate-1"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    html = r.read().decode("utf-8", errors="ignore")
                # Trending repos: <h2 class="h3 lh-condensed"><a href="/owner/repo">
                for m in re.finditer(r'<a href="/([\w.-]+/[\w.-]+)" data-view-component="true" class="Link"', html):
                    full = m.group(1)
                    if full.count("/") != 1: continue
                    if add_to_frontier(full, 0.95, f"trending:{lang or 'all'}-{ttl}"):
                        added += 1
            except Exception as e:
                log(f"  trending fetch err {type(e).__name__}")
    return added


# ── Main scheduler — round-robin all worker types ──────────────────────────
def main(max_runtime_sec: int = 0) -> None:
    if not TOKEN_POOL:
        log("ERR: GITHUB_TOKEN_POOL not set — exit")
        return
    init_db()
    pool = TokenPool(TOKEN_POOL)
    log(f"start | tokens={len(TOKEN_POOL)} | total_quota_per_h={len(TOKEN_POOL) * 5000}")

    started_at = time.time()
    cycle = 0
    while True:
        if max_runtime_sec > 0 and time.time() - started_at > max_runtime_sec:
            log(f"runtime limit hit ({max_runtime_sec}s) — exit")
            break
        cycle += 1
        log(f"=== cycle {cycle} | quota_remaining={pool.total_remaining()} ===")

        # 1. Seeding (every 10 cycles)
        if cycle % 10 == 1:
            n_aw = worker_awesome_seeds(pool)
            log(f"  awesome-seeds: +{n_aw} repos to frontier")
        if cycle % 5 == 1:
            n_tr = worker_trending(pool)
            log(f"  trending: +{n_tr} repos to frontier")

        # 2. Topic search (3-4 queries per cycle)
        for q in random.sample(SEARCH_QUERIES, min(4, len(SEARCH_QUERIES))):
            n = worker_topic_search(pool, q)
            log(f"  search '{q[:40]}...': +{n}")

        # 3. Repo deep-dive (8 in parallel)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = []
            for _ in range(8):
                full = take_from_frontier()
                if full is None: break
                futures.append(ex.submit(worker_repo_deepdive, pool, full))
            results = [f.result() for f in as_completed(futures, timeout=600)]
            log(f"  deep-dive: {len(results)} repos | pairs={sum(results)} | quota_left={pool.total_remaining()}")

        # 4. Adaptive cool-down based on quota
        remaining = pool.total_remaining()
        if remaining < 500:
            wait = max(60, int(pool.soonest_reset() - time.time()))
            log(f"  low quota ({remaining}) — sleep {min(wait, 900)}s")
            time.sleep(min(wait, 900))
        elif remaining < 5000:
            time.sleep(30)
        else:
            time.sleep(5)

        # 5. Stats
        with sqlite3.connect(DB) as c:
            v = c.execute("SELECT COUNT(*) FROM repos_visited").fetchone()[0]
            f = c.execute("SELECT COUNT(*) FROM repo_frontier").fetchone()[0]
            p = c.execute("SELECT COUNT(*) FROM prs_visited").fetchone()[0]
            i = c.execute("SELECT COUNT(*) FROM issues_visited").fetchone()[0]
        log(f"  cumulative: visited={v} frontier={f} prs={p} issues={i}")


if __name__ == "__main__":
    runtime = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    main(runtime)
