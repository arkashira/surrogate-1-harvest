#!/usr/bin/env python3
"""
Local RAG assistant — SQLite FTS5 (replaces Chroma) + local LLM.
Stable, no Rust crashes, fast.

Usage:
  ask-sqlite.py "คำถาม"              # single shot
  ask-sqlite.py -i                    # interactive
  ask-sqlite.py --source code "คำถาม"  # filter by source
  ask-sqlite.py --project Vanguard "คำถาม"
"""
import sys, json, sqlite3, argparse, subprocess, urllib.request, re
from pathlib import Path

DB = str(Path.home() / ".claude/index.db")
OLLAMA = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "granite4:7b-a1b-h"

AXENTX = Path("/Users/Ashira/axentx")
PROJECTS = ["Costinel", "Vanguard", "arkship", "surrogate", "workio"]


def fts_escape(query: str) -> str:
    """Turn a natural query into FTS5 MATCH syntax — use each non-trivial word."""
    words = re.findall(r"\w{3,}", query)  # keep alnum words ≥3 chars
    if not words: return '"placeholder"'
    # OR query for flexibility
    return " OR ".join(f'"{w}"' for w in words[:10])


def search(query: str, n: int = 10, source: str = None, project: str = None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    fts_q = fts_escape(query)
    sql = """
        SELECT d.source, d.project, d.path, d.topic, d.instruction, d.response,
               rank
        FROM docs_fts f JOIN docs d ON f.rowid = d.id
        WHERE docs_fts MATCH ?
    """
    params = [fts_q]
    if source:
        sql += " AND d.source LIKE ?"
        params.append(f"%{source}%")
    if project:
        sql += " AND d.project LIKE ?"
        params.append(f"%{project}%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(n)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS syntax error — fallback to LIKE
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source, project, path, topic, instruction, response FROM docs "
            "WHERE instruction LIKE ? OR response LIKE ? LIMIT ?",
            (f"%{query[:80]}%", f"%{query[:80]}%", n)
        ).fetchall()
    return rows


def agents_md() -> str:
    parts = []
    for proj in PROJECTS:
        md = AXENTX / proj / "AGENTS.md"
        if md.exists():
            parts.append(f"=== {proj}/AGENTS.md ===\n" + "\n".join(md.read_text().split("\n")[:15]))
    return "\n\n".join(parts)


def git_recent() -> str:
    out = []
    for proj in PROJECTS:
        p = AXENTX / proj
        if not (p / ".git").exists(): continue
        try:
            r = subprocess.run(["git","-C",str(p),"log","--oneline","-5"],
                             capture_output=True, text=True, timeout=3)
            if r.stdout.strip():
                out.append(f"=== {proj} ===\n{r.stdout.strip()}")
        except: pass
    return "\n".join(out)


def build_context(question, source=None, project=None):
    parts = ["## AGENTS.md\n" + agents_md()]
    g = git_recent()
    if g: parts.append("## Recent commits\n" + g)

    rows = search(question, n=8, source=source, project=project)
    if rows:
        hits = []
        for r in rows:
            tag = r["source"] or "?"
            path = r["path"] or ""
            proj = r["project"] or ""
            content = r["response"] or r["instruction"] or ""
            hits.append(f"[{tag}:{proj}/{path[-60:]}]\n{content[:500]}")
        parts.append(f"## Relevant docs (SQLite FTS, {len(rows)} matches)\n" + "\n\n".join(hits))
    return "\n\n".join(parts)[:12000]


SYSTEM_PROMPT = (
    "คุณคือ local assistant ตอบจาก Context เท่านั้น. ไม่รู้ก็บอก. "
    "ภาษาไทย กระชับ. อ้าง path/source ที่เกี่ยวข้อง."
)


def ask_ollama(messages, model):
    payload = {"model": model, "messages": messages, "stream": False}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read()).get("message", {}).get("content", "(no response)")


def single(question, model, source, project):
    print(f"🔍 SQLite FTS search...", file=sys.stderr)
    ctx = build_context(question, source, project)
    print(f"   context: {len(ctx)} chars", file=sys.stderr)
    print(f"🤖 {model}\n", file=sys.stderr)
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"### Context\n{ctx}\n\n### คำถาม\n{question}"},
    ]
    print(ask_ollama(msgs, model))


def interactive(model, source, project):
    print(f"🤖 Interactive — {model}, source={source}, project={project}", file=sys.stderr)
    print(f"   type 'exit' to quit, ':s <src>' to set source filter", file=sys.stderr)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    base_ctx = None
    while True:
        try: q = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt): break
        if not q or q in ("exit","quit"): break
        if q.startswith(":s "):
            source = q[3:].strip() or None
            print(f"  source filter: {source}")
            continue

        ctx = build_context(q, source, project)
        msgs = history + [{"role": "user", "content": f"### Context\n{ctx}\n\n### คำถาม\n{q}"}]
        ans = ask_ollama(msgs, model)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": ans})
        print(f"\n{ans}\n")
        if len(history) > 11:
            history = [history[0]] + history[-10:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--interactive", action="store_true")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("--source", help="filter by source (code, github-public, claude-conversation, ...)")
    ap.add_argument("--project", help="filter by project")
    ap.add_argument("question", nargs="*")
    args = ap.parse_args()

    if args.interactive:
        interactive(args.model, args.source, args.project)
    else:
        if not args.question:
            print("usage: ask 'คำถาม' OR ask -i OR ask --source code 'คำถาม'", file=sys.stderr)
            sys.exit(1)
        single(" ".join(args.question), args.model, args.source, args.project)


if __name__ == "__main__":
    main()
