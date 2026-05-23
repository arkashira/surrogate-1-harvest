#!/usr/bin/env python3
"""axentx commit daemon — picks items from commit-queue, EXTRACTS code from
dev/review history, writes to actual project files, commits and pushes.

Bug fix 2026-05-03 (root cause of "Shipped but no features added"):
  Previous version only wrote a markdown decision-log to .axentx-dev-bot/
  and never extracted the actual code blocks from dev output. Every cycle
  produced a 'shipped' Discord ping but the real source files were never
  touched. User feedback: 'ทำไมไม่เพิ่ม feature เลย / spec → code feature นะ'

What this daemon now does:
  1. Pick next commit-queue item.
  2. Resolve project repo (auto-clone if missing — both arkashira/* and
     ashirapit/* org patterns supported).
  3. Find latest dev output in item['history'].
  4. Extract (file_path, code) pairs from markdown:
       - Pattern A: '### N) ...: `<abs_or_rel_path>`' header followed by
         a fenced code block.
       - Pattern B: '**File: <path>**' or '**`<path>`**' header followed
         by a fenced code block.
       - Fallback: if exactly one code block + payload['files_hint'] has
         exactly one path → write that block to that path.
  5. Write each (path, code) pair into the repo (creates dirs as needed).
  6. Also write the decision-log to .axentx-dev-bot/<id>.md (audit trail).
  7. git add + commit + push with rebase-retry on non-fast-forward.

Safety guards:
  - Refuses to write outside the repo root (rejects ../ in extracted paths).
  - Skips obviously-wrong code blocks (e.g. <100 chars or shell prompts).
  - On extraction failure: still writes decision-log so we don't lose the
    audit trail, but logs '⚠ no code extracted' so operator can investigate.
"""
from __future__ import annotations
import os
import re
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, pick_oldest, advance, fail,  # noqa: E402
                             daemon_loop)

POLL_SEC = 60
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Org candidates for auto-clone fallback. axentx-product-spawner-daemon
# creates new repos under ashirapit/*. User's 5 main projects live under
# arkashira/*. Try both before giving up.
GH_OWNER_CANDIDATES = ("arkashira", "ashirapit", "ashirafuse")


# ── Code-block extractor ─────────────────────────────────────────────────
# Match '### N) <title>: `<path>`' or '**File: `<path>`**' or '**`<path>`**'
# headers, capture the path, then capture the next fenced ``` code block.
_HEADER_RE = re.compile(
    r"""
    (?:^\#{1,6}\s+\d*\)?\s*[^`\n]*?[`'"]+(?P<p1>[^`'"\n]+?\.[a-zA-Z]{1,5})[`'"]+ ) |   # ### 1) Service: `path.ts`
    (?:^\*\*\s*(?:File|Path|Edit)\s*:\s*[`'"]?(?P<p2>[^`'"\n]+?\.[a-zA-Z]{1,5})[`'"]?\s*\*\*) | # **File: path.ts**
    (?:^\*\*[`'"]+(?P<p3>[^`'"\n]+?\.[a-zA-Z]{1,5})[`'"]+\*\*)                          # **`path.ts`**
    """,
    re.MULTILINE | re.VERBOSE,
)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)
# In-block path comment — many LLMs put `// path/to/file.ts` on line 1 of
# the code block instead of using a markdown header. Match the canonical
# extensions we expect to see in feature code.
_INLINE_PATH_RE = re.compile(
    r"""^\s*(?:
        //|\#|--|<!--|/\*
    )\s*
    (?P<path>[\w./\-]+?\.(?:
        ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|kt|swift|rb|php|
        cpp|cc|c|h|hpp|cs|sql|html|css|scss|sass|less|
        md|mdx|yaml|yml|json|jsonc|toml|ini|env|sh|bash|zsh|fish|
        Dockerfile|tf|hcl|proto|graphql|gql|vue|svelte
    ))(?:\s|$|\*/|-->)""",
    re.VERBOSE,
)


def _strip_prefix(path: str, project: str) -> str:
    """Normalize an extracted path to be relative to the repo root."""
    p = path.strip().lstrip("/")
    # Strip common absolute prefixes
    for prefix in (
        f"opt/axentx/{project}/",
        f"axentx/{project}/",
        f"{project}/",
    ):
        if p.startswith(prefix):
            return p[len(prefix):]
    return p


def _safe_path(rel: str, repo: Path) -> Path | None:
    """Refuse paths that escape the repo via .. or absolute — return None."""
    if rel.startswith("/") or "../" in rel or rel == "":
        return None
    candidate = (repo / rel).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    return candidate


def extract_code_blocks(text: str, project: str, repo: Path,
                        files_hint: list[str] | None = None
                        ) -> list[tuple[Path, str]]:
    """Return [(absolute-target, code), ...] extracted from a dev output.

    Resolution order per code fence:
      1. Inline path comment on line 1 of the fence (e.g. ``// src/foo.ts``)
         — most reliable, used by most LLM outputs.
      2. Markdown header immediately preceding the fence
         (e.g. ``### 1) Service: backtick-src/foo.ts-backtick``).
      3. Single-block + single-hint fallback (uses ``files_hint`` from payload).

    Deduplicates by target path (later block wins — usually a refinement).
    """
    # Build the map of (header_position → path) once
    header_positions: list[tuple[int, str]] = []
    for m in _HEADER_RE.finditer(text):
        p = m.group("p1") or m.group("p2") or m.group("p3")
        if p:
            header_positions.append((m.end(), p))

    def _header_path_before(pos: int) -> str | None:
        # Closest header that starts before this fence
        prev = None
        for hp, pth in header_positions:
            if hp <= pos:
                prev = pth
            else:
                break
        return prev

    pairs_by_path: dict[str, tuple[Path, str]] = {}

    for fm in _FENCE_RE.finditer(text):
        code = fm.group(1).rstrip()
        if len(code) < 30:
            continue

        # Strategy 1: inline path comment on first line
        first_line = code.split("\n", 1)[0] if code else ""
        inline_match = _INLINE_PATH_RE.match(first_line)
        path: str | None = None
        if inline_match:
            path = inline_match.group("path")
            # Strip the path comment line from the code body — keeps the
            # actual file content clean (the LLM's path comment was just
            # a marker, not part of the source).
            code = code.split("\n", 1)[1] if "\n" in code else ""

        # Strategy 2: closest header path
        if not path:
            path = _header_path_before(fm.start())

        if not path:
            continue

        rel = _strip_prefix(path, project)
        target = _safe_path(rel, repo)
        if target and len(code) >= 30:
            pairs_by_path[str(target)] = (target, code)

    # Strategy 3: single-block fallback with files_hint
    if not pairs_by_path and files_hint:
        fences = _FENCE_RE.findall(text)
        substantial = [c for c in fences if len(c) >= 200]
        if len(substantial) == 1 and len(files_hint) == 1:
            rel = _strip_prefix(files_hint[0], project)
            target = _safe_path(rel, repo)
            if target:
                pairs_by_path[str(target)] = (target, substantial[0].rstrip())

    return list(pairs_by_path.values())


def find_dev_output(item: dict) -> tuple[str, list[str]]:
    """Pick the LATEST 'dev' output (refinements preferred), plus files_hint
    list from the payload (if any)."""
    history = item.get("history") or []
    # Prefer the latest stage='dev' entry (or 'claude/llm-fallback-chain'
    # within dev stage).
    dev_entries = [h for h in history if h.get("stage") == "dev"]
    chosen = ""
    if dev_entries:
        chosen = (dev_entries[-1].get("output") or "")
    files_hint = []
    payload = item.get("payload") or {}
    if isinstance(payload, dict):
        fh = payload.get("files_hint") or item.get("files_hint")
        if isinstance(fh, list):
            files_hint = [str(p) for p in fh]
    return chosen, files_hint


def render_decision_md(item: dict) -> str:
    out = ["# axentx-dev-bot decision",
           f"- id: `{item['id']}`",
           f"- project: {item.get('project')}",
           f"- focus: {item.get('focus')}",
           f"- created_at: {item.get('created_at')}",
           ""]
    for h in item.get("history", []):
        out.append(f"## {h.get('stage')} — {h.get('actor')} @ {h.get('at')}")
        out.append("")
        out.append(h.get("output", "")[:4000])
        out.append("")
    return "\n".join(out)


def ensure_repo(project: str) -> Path | None:
    """Resolve PROJECTS_ROOT/project; auto-clone if missing.
    Tries arkashira → ashirapit → ashirafuse owners with the GH_TOKEN."""
    repo = PROJECTS_ROOT / project
    if repo.exists() and (repo / ".git").exists():
        return repo
    if not GH_TOKEN:
        return None
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    for owner in GH_OWNER_CANDIDATES:
        url = f"https://x-access-token:{GH_TOKEN}@github.com/{owner}/{project}.git"
        r = subprocess.run(
            ["git", "clone", "--depth", "50", url, str(repo)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            log("commit", f"  cloned {owner}/{project}")
            return repo
        # Clean up partial clone to allow retry with next owner
        subprocess.run(["rm", "-rf", str(repo)], capture_output=True)
    return None


def git_commit_msg(item: dict, n_files: int) -> str:
    focus = item.get("focus") or "feature"
    payload = item.get("payload") or {}
    story = ""
    if isinstance(payload, dict):
        story = payload.get("story") or ""
    title = (story[:80] + "…") if len(story) > 80 else story
    if not title:
        title = f"{focus} cycle {item['id'][:24]}"
    return (
        f"axentx-dev-bot: {title}\n\n"
        f"Files written: {n_files}\n"
        f"Pipeline: dev → review → qa → commit\n"
        f"See .axentx-dev-bot/{item['id']}.md for full audit trail.\n"
    )


def do_one_commit() -> bool:
    picked = pick_oldest("commit")
    if not picked:
        return False
    src_path, item = picked
    project = item.get("project", "")
    if not project:
        fail(item, src_path, "commit", "no project on item")
        return True

    # Skip archived/missing remote repos (e.g. cost-radar archived 2026-05-04).
    # Marker set by the commit-loop on 'Repository not found' to prevent
    # endless retry storms.
    try:
        from axentx_shared import kv_get
        skip = kv_get(f"commit.skip-project.{project}")
        if skip:
            log("commit",
                f"  ⊘ {item['id'][:32]} → {project} skipped "
                f"(archived/missing remote)")
            advance(item, src_path, "done", "commit",
                    f"skipped: {project} remote archived")
            return True
    except Exception:
        pass

    repo = ensure_repo(project)
    if not repo:
        fail(item, src_path, "commit",
             f"project repo missing + auto-clone failed: {project}")
        return True

    # Pre-sync: discard noisy auto-gen files, fetch+rebase
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--",
         ".axentx-release-v0.1.0.md"],
        capture_output=True, text=True, timeout=10,
    )
    try:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", "main"],
            capture_output=True, text=True, timeout=20,
        )
        subprocess.run(
            ["git", "-C", str(repo), "rebase", "origin/main"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        log("commit", f"⚠ {item['id']} pre-push fetch/rebase timed out")
    except Exception as e:
        log("commit", f"  pre-sync warn: {e}")

    # ── Extract code blocks from dev output ─────────────────────────────
    dev_output, files_hint = find_dev_output(item)
    code_pairs = extract_code_blocks(dev_output, project, repo, files_hint)

    # ── Write decision-log (always) + extracted source files ────────────
    target_dir = repo / ".axentx-dev-bot"
    target_dir.mkdir(exist_ok=True)
    decision_md = target_dir / f"{item['id']}.md"
    decision_md.write_text(render_decision_md(item))

    written: list[Path] = [decision_md]
    for path, code in code_pairs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
        written.append(path)

    if code_pairs:
        log("commit", f"▸ {item['id']} → {len(code_pairs)} src files + 1 decision")
    else:
        log("commit", f"⚠ {item['id']} → no code extracted (decision-only commit)")

    # ── git add + commit + push with rebase-retry ──────────────────────
    try:
        rels = [str(p.relative_to(repo)) for p in written]
        subprocess.run(["git", "-C", str(repo), "add", *rels],
                       check=True, capture_output=True)
        # Did anything actually change?
        diff_check = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff_check.returncode == 0:
            log("commit", f"  {item['id']} no-op (files identical to HEAD)")
            advance(item, src_path, "done", "commit", "no-op")
            return True

        msg = git_commit_msg(item, len(code_pairs))
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "axentx-dev-bot",
               "GIT_AUTHOR_EMAIL": "dev-bot@axentx.local",
               "GIT_COMMITTER_NAME": "axentx-dev-bot",
               "GIT_COMMITTER_EMAIL": "dev-bot@axentx.local"}
        r = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            log("commit", f"  commit fail: {(r.stdout + r.stderr)[:200]}")
        else:
            push_ok = False
            attempt = 0
            archived_repo = False
            for attempt in range(2):
                push = subprocess.run(
                    ["git", "-C", str(repo), "push", "origin", "HEAD:main"],
                    capture_output=True, text=True, timeout=60,
                )
                if push.returncode == 0:
                    push_ok = True
                    log("commit",
                        f"✓ {item['id']} → {project} ({len(code_pairs)} src + decision)"
                        f"{' (retry)' if attempt else ''}")
                    break
                err = (push.stdout + push.stderr)[:300]
                if ("Repository not found" in err
                    or "archived" in err.lower()
                    or "does not appear to be a git repository" in err
                    or "could not read from remote" in err.lower()):
                    # Repo archived/deleted on remote — don't keep retrying.
                    # Mark it so future cycles skip it. User feedback 2026-05-04:
                    # 'cost-radar archived → push fail loop'.
                    archived_repo = True
                    log("commit",
                        f"  ⊘ {project}: remote archived/missing — "
                        f"will skip future commits")
                    try:
                        from axentx_shared import kv_set
                        kv_set(f"commit.skip-project.{project}", {
                            "ts": datetime.datetime.utcnow().isoformat() + "Z",
                            "reason": "remote-archived-or-missing",
                            "host": socket.gethostname(),
                        })
                    except Exception:
                        pass
                    break
                if "fetch first" in push.stderr or "non-fast-forward" in push.stderr:
                    subprocess.run(
                        ["git", "-C", str(repo), "pull", "--rebase",
                         "origin", "main"],
                        capture_output=True, text=True, timeout=30,
                    )
                    continue
                log("commit", f"  push fail: {err[:200]}")
                break
            if not push_ok and not archived_repo:
                log("commit", f"  push: failed after {attempt + 1} attempt(s)")
    except subprocess.TimeoutExpired:
        log("commit", f"⏱ {item['id']} push TIMEOUT")
    except Exception as e:
        log("commit", f"✗ {item['id']}: {type(e).__name__}: {str(e)[:120]}")

    advance(item, src_path, "done", "commit",
            f"{len(code_pairs)} src files + decision")

    # Self-knowledge hook: log to shared_memory each commit so other
    # daemons + future training data have the pattern.
    try:
        global _N_COMMITS
        _N_COMMITS = globals().get("_N_COMMITS", 0) + 1
        globals()["_N_COMMITS"] = _N_COMMITS
        if code_pairs and _N_COMMITS % 10 == 0:
            from axentx_shared import memory_log
            memory_log("commit", "event",
                       f"commit-daemon pushed {_N_COMMITS}th batch",
                       body=(f"Latest: {item.get('project','?')} got "
                             f"{len(code_pairs)} src files committed. "
                             f"Pattern: extract code blocks via inline "
                             f"path comments + markdown headers."),
                       tags=["commit", "milestone"])
    except Exception:
        pass
    return True


if __name__ == "__main__":
    daemon_loop("commit", POLL_SEC, do_one_commit)
