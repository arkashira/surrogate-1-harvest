"""
Repo context builder — anti-rework via tree-sitter symbol map + relevance retrieval.

When orchestrate runs in a repo, we inject:
  1. File tree (top 100 files by relevance)
  2. Symbol index (functions/classes from related files)
  3. Imports + dependencies
  4. README excerpt
  → DEV stage gets THIS context as part of prompt → fewer hallucinated imports,
    better matching of existing patterns, less rework.

Caches per-cwd to avoid re-scanning. Invalidated on .git HEAD change.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".surrogate/state/repo-context-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _git_head(cwd: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()[:12] if r.returncode == 0 else ""
    except Exception:
        return ""


def _cache_key(cwd: Path, query: str) -> str:
    head = _git_head(cwd)
    raw = f"{cwd}|{head}|{query[:200]}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _list_relevant_files(cwd: Path, query: str, limit: int = 30) -> list[Path]:
    """Pick top-N files relevant to query via filename + content keyword match."""
    if not cwd.exists():
        return []
    keywords = set()
    for w in re.findall(r'\b[a-zA-Z][a-zA-Z0-9_]{3,}\b', query.lower()):
        if w not in {"the", "this", "that", "with", "from", "into",
                     "what", "when", "where", "function", "class", "method"}:
            keywords.add(w)
    candidates: list[tuple[Path, int]] = []
    for ext in ("py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "kt", "rb",
                "swift", "c", "cpp", "h", "hpp", "cs", "php", "sh", "yaml", "yml",
                "json", "toml", "md"):
        for f in cwd.rglob(f"*.{ext}"):
            # Skip noise
            sp = str(f)
            if any(skip in sp for skip in ("/node_modules/", "/.venv/", "/__pycache__/",
                                            "/dist/", "/build/", "/.git/",
                                            "/.next/", "/target/", "/vendor/")):
                continue
            score = 0
            name_low = f.name.lower()
            for kw in keywords:
                if kw in name_low:
                    score += 10
            try:
                if f.stat().st_size < 200_000:
                    snippet = f.read_text(errors="ignore")[:5000].lower()
                    for kw in keywords:
                        score += snippet.count(kw)
            except Exception:
                pass
            if score > 0:
                candidates.append((f, score))
    candidates.sort(key=lambda x: -x[1])
    return [f for f, _ in candidates[:limit]]


def _extract_symbols(file: Path, max_chars: int = 3000) -> str:
    """Pull function/class signatures from a file (tree-sitter-lite via regex)."""
    try:
        text = file.read_text(errors="ignore")[:max_chars * 4]
    except Exception:
        return ""
    sigs: list[str] = []
    # Python: def / class / async def
    for m in re.finditer(r'^(async\s+def|def|class)\s+(\w+)[^:]*:', text, re.MULTILINE):
        sigs.append(m.group(0).strip())
    # TypeScript/JavaScript: function / const / interface / type / class
    for m in re.finditer(r'^(export\s+)?(async\s+)?function\s+(\w+)\s*\([^)]*\)', text, re.MULTILINE):
        sigs.append(m.group(0).strip())
    for m in re.finditer(r'^(export\s+)?(interface|type|class)\s+(\w+)', text, re.MULTILINE):
        sigs.append(m.group(0).strip())
    # Go: func
    for m in re.finditer(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(', text, re.MULTILINE):
        sigs.append(m.group(0).strip())
    # Rust: fn / pub fn / impl
    for m in re.finditer(r'^(pub\s+)?(async\s+)?fn\s+(\w+)\s*[<(]', text, re.MULTILINE):
        sigs.append(m.group(0).strip())
    return "\n".join(sigs[:60])[:max_chars]


def build_context(cwd: str | Path, query: str, max_kb: int = 30) -> str:
    """Returns markdown-formatted repo context for injection into orchestrate prompt."""
    cwd = Path(cwd).resolve()
    if not cwd.exists():
        return ""
    cache_key = _cache_key(cwd, query)
    cache_file = CACHE_DIR / f"{cache_key}.txt"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 1800:
        return cache_file.read_text()

    parts: list[str] = []
    parts.append(f"## Repo: {cwd.name} (HEAD: {_git_head(cwd) or 'no-git'})")

    # README excerpt
    for readme in ("README.md", "README", "readme.md"):
        rp = cwd / readme
        if rp.exists():
            try:
                excerpt = rp.read_text(errors="ignore")[:1500]
                parts.append(f"### README\n{excerpt}")
                break
            except Exception:
                pass

    # Project config (package.json / pyproject.toml / Cargo.toml / go.mod)
    for cfg in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "build.gradle.kts"):
        cp = cwd / cfg
        if cp.exists():
            try:
                excerpt = cp.read_text(errors="ignore")[:1000]
                parts.append(f"### {cfg}\n```\n{excerpt}\n```")
                break
            except Exception:
                pass

    # Top relevant files
    relevant = _list_relevant_files(cwd, query, limit=15)
    if relevant:
        parts.append(f"### Top {len(relevant)} relevant files (by query match):")
        for f in relevant:
            rel = f.relative_to(cwd)
            sigs = _extract_symbols(f)
            parts.append(f"\n**{rel}**")
            if sigs:
                parts.append(f"```\n{sigs}\n```")

    out = "\n".join(parts)
    # Cap total size
    out = out[:max_kb * 1024]
    cache_file.write_text(out)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: repo_context.py <cwd> <query>", file=sys.stderr)
        sys.exit(2)
    print(build_context(sys.argv[1], sys.argv[2]))
