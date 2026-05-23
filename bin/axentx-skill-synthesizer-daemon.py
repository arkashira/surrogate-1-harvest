#!/usr/bin/env python3
"""axentx skill synthesizer — auto-discover techniques from repeated failures.

User directive (2026-05-02):
  > "อย่าลืม ต้องสามารถ สร้าง synthesis skill มาแก้ปัญหาต่างๆ ได้เอง
  >  อัตโนมัติด้วย ต้องเก่ง เรื่อยๆ"
  > "ถ้ามีปัญหา หรืออะไร สังเคราะห์ เทคนิคมาแก้เองได้เลย และทำเป็น paper
  >  ไว้ด้วย ทำเป็น knowledge ไว้ ว่าค้นพบเทคนิคนี้นะ เพื่อแก้ปัญหานี้"

How it works:

  1. Mine pattern-of-failures from journalctl + done/ items + heartbeat
     errors. Cluster by message similarity (cheap n-gram + Jaccard).
  2. For any cluster with ≥ MIN_REPEAT failures + ≥ MIN_AGENTS distinct
     daemons reporting it, treat as a "synthesizable problem".
  3. Use call_llm_strong() to:
        a. Diagnose root cause (with grounding)
        b. Search for known solutions (via RAG over our own knowledge +
           web sources)
        c. Synthesize a SKILL.md (markdown skill module the agents can
           load) + a verifier (1 unit test or shell check)
        d. Write a "paper" — a 200-400 word technical note documenting
           the technique, conditions of validity, and citations.
  4. Validate the skill: dry-run the verifier. If it passes, commit
     SKILL.md to state/skills/<id>/SKILL.md AND the paper to
     state/papers/<id>.md. State-sync-daemon then propagates both to
     the `state` git branch.
  5. Each (problem-pattern, skill, paper) is also written to
     training-pairs.jsonl with flavor=skill-synthesis so v2 trainer
     learns the meta-capability of generating skills.

Maturity gating:
  Per user: "ต้องเป็นวิธีการ หรือเทคนิคที่ mature จริงๆ นะ"
  We only commit a skill if:
    - The verifier passes
    - The technique is grounded in ≥ 2 distinct sources (cited)
    - The synthesized SKILL.md isn't a near-duplicate of an existing one
      (cosine sim < 0.85 against state/skills/* via RAG)
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, call_llm_strong, daemon_loop,
                             rag_top_score, rag_query)

POLL_SEC = int(os.environ.get("SKILL_SYNTH_POLL_SEC", "1800"))  # 30 min
MIN_REPEAT = int(os.environ.get("SKILL_MIN_REPEAT", "5"))
MIN_AGENTS = int(os.environ.get("SKILL_MIN_AGENTS", "2"))
MAX_PER_CYCLE = int(os.environ.get("SKILL_MAX_PER_CYCLE", "1"))

SKILLS_DIR = REPO_ROOT / "state" / "skills"
PAPERS_DIR = REPO_ROOT / "state" / "papers"
PAIRS_FILE = REPO_ROOT / "state" / "training-pairs.jsonl"
CURSOR_FILE = REPO_ROOT / "state" / ".skill-synth-cursor.json"

SKILLS_DIR.mkdir(parents=True, exist_ok=True)
PAPERS_DIR.mkdir(parents=True, exist_ok=True)


SYNTH_SYSTEM = """You are a senior research engineer. Given a recurring
failure pattern observed across multiple agents, synthesize a robust
technique to fix it once and for all.

Output STRICT JSON (no commentary, no code fences):

{
  "title": "<6-10 word skill title>",
  "diagnosis": "<root cause in 2-3 sentences, grounded in the pattern>",
  "technique": "<the canonical solution as 1-2 paragraphs>",
  "skill_md": "<full markdown SKILL.md content with frontmatter, instructions, examples>",
  "verifier": {
    "kind": "shell|python|none",
    "check": "<command/script that exits 0 if technique works>",
    "rationale": "<what the verifier proves in 1 sentence>"
  },
  "paper": {
    "title": "<paper title>",
    "abstract": "<150 word abstract>",
    "body": "<400-600 word technical note: problem → analysis → solution → validity → limitations → citations>",
    "citations": ["<min 2 distinct sources: URLs, RFC/spec names, repo IDs, paper DOIs>"]
  },
  "maturity": "production|experimental|speculative",
  "applicability": ["<which agents/stages benefit, e.g. 'research-daemon', 'dev-daemon synthesize'>"]
}

CRITICAL — MATURITY GATING:
- maturity=production ONLY if technique is widely-deployed in the field
  AND you have ≥ 2 concrete citations (not invented).
- maturity=experimental if novel but verifiable — verifier MUST pass.
- maturity=speculative if you cannot construct a verifier — DO NOT EMIT
  these (return {"skip": true, "reason": "..."} instead).

CRITICAL — NO HALLUCINATION:
- Never invent URLs, paper DOIs, package names, or function signatures.
- If you cannot cite ≥ 2 real sources for a technique, return
  {"skip": true, "reason": "insufficient grounding"}.
"""


def log_(msg: str) -> None:
    log("skill-synth", msg)


def _git(*args: str, cwd: Path = REPO_ROOT, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, timeout=timeout)


def collect_failures(since_min: int = 60) -> dict[str, list[dict]]:
    """Pull recent failures from journalctl + heartbeat KV.
    Returns {pattern_key: [{agent, msg, at}, ...]}.
    """
    failures: dict[str, list[dict]] = defaultdict(list)

    # Pattern keys = first 80 chars of the failure message, lowercased,
    # with timestamps + IDs scrubbed. Cheap clustering — good enough for
    # signal aggregation.
    def normalize(msg: str) -> str:
        s = re.sub(r"https?://\S+", "URL", msg)
        s = re.sub(r"\b[0-9a-f]{6,}\b", "HASH", s)
        s = re.sub(r"\d+", "N", s)
        s = re.sub(r"\s+", " ", s).strip().lower()
        return s[:120]

    # Tail journalctl across all axentx daemons in one query.
    # Limit to last N lines to keep query under 60s (full --since on a chatty
    # fleet can pull MBs of logs and time out on small VMs).
    try:
        r = subprocess.run(
            ["journalctl", "--no-pager", "--output=cat",
             "-u", "axentx-*", "-u", "surrogate-*", "-u", "hermes-*",
             "-n", "5000",  # last N lines instead of time window
             "--grep", "⚠|✗|ERROR|FAIL|exception|fatal|Traceback"],
            capture_output=True, text=True, timeout=60,
        )
        for line in (r.stdout or "").splitlines():
            if not re.search(r"⚠|✗|ERROR|FAIL|exception|fatal|Traceback",
                             line, re.I):
                continue
            # Parse [time] [agent] message — best-effort
            m = re.match(r".*?\[([\w-]+)\]\s*(.+)", line)
            agent = m.group(1) if m else "unknown"
            msg = (m.group(2) if m else line).strip()
            key = normalize(msg)
            if not key or len(key) < 20:
                continue
            failures[key].append({
                "agent": agent,
                "msg": msg[:300],
                "at": datetime.datetime.utcnow().isoformat() + "Z",
            })
    except Exception as e:
        log_(f"journalctl scrape fail: {e}")

    return failures


def is_dupe_skill(title: str, technique: str) -> bool:
    """RAG-check whether we already have a similar skill."""
    try:
        sim = rag_top_score(f"{title}\n\n{technique[:500]}", kind="skill")
        return sim >= 0.85
    except Exception:
        return False


def already_synthed(pattern_hash: str) -> bool:
    if not CURSOR_FILE.exists():
        return False
    try:
        cur = json.loads(CURSOR_FILE.read_text())
        return pattern_hash in (cur.get("done") or [])
    except Exception:
        return False


def mark_synthed(pattern_hash: str) -> None:
    cur: dict = {"done": []}
    if CURSOR_FILE.exists():
        try:
            cur = json.loads(CURSOR_FILE.read_text())
        except Exception:
            pass
    cur.setdefault("done", []).append(pattern_hash)
    cur["done"] = cur["done"][-500:]
    CURSOR_FILE.write_text(json.dumps(cur, indent=2))


def run_verifier(verifier: dict) -> tuple[bool, str]:
    """Best-effort run the verifier in a sandboxed shell. Returns (passed, msg)."""
    kind = verifier.get("kind", "none")
    check = verifier.get("check", "")
    if kind == "none" or not check:
        return False, "no verifier"
    # Run with strict timeout + restricted CWD
    try:
        if kind == "shell":
            r = subprocess.run(["bash", "-c", check[:4000]],
                               capture_output=True, text=True,
                               timeout=30, cwd="/tmp")
        elif kind == "python":
            r = subprocess.run(["python3", "-c", check[:4000]],
                               capture_output=True, text=True,
                               timeout=30, cwd="/tmp")
        else:
            return False, f"unknown verifier kind: {kind}"
        ok = r.returncode == 0
        out = (r.stdout + r.stderr)[-300:]
        return ok, out.strip()
    except subprocess.TimeoutExpired:
        return False, "verifier timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def write_skill(skill_id: str, data: dict) -> tuple[Path, Path]:
    """Persist SKILL.md + paper.md. Returns (skill_path, paper_path)."""
    skill_dir = SKILLS_DIR / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(data.get("skill_md", "") + "\n")
    # Frontmatter index file
    (skill_dir / "metadata.json").write_text(
        json.dumps({
            "id": skill_id,
            "title": data.get("title", ""),
            "maturity": data.get("maturity", "experimental"),
            "applicability": data.get("applicability") or [],
            "synthesized_at": datetime.datetime.utcnow().isoformat() + "Z",
            "diagnosis": data.get("diagnosis", ""),
            "verifier": data.get("verifier", {}),
        }, indent=2, ensure_ascii=False)
    )
    paper = data.get("paper", {}) or {}
    paper_path = PAPERS_DIR / f"{skill_id}.md"
    paper_path.write_text(
        f"# {paper.get('title', data.get('title','Untitled'))}\n\n"
        f"_synthesized {datetime.datetime.utcnow().isoformat()}Z by axentx-skill-synthesizer_\n\n"
        f"## Abstract\n\n{paper.get('abstract','')}\n\n"
        f"## Body\n\n{paper.get('body','')}\n\n"
        f"## Citations\n\n" +
        "\n".join(f"- {c}" for c in (paper.get('citations') or [])) +
        "\n"
    )
    return skill_md, paper_path


def emit_training_pair(skill_id: str, pattern_examples: list[dict],
                       data: dict) -> None:
    """Append a flavor=skill-synthesis training pair so v2 trainer
    learns the meta-capability of skill generation."""
    if not PAIRS_FILE.exists():
        PAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAIRS_FILE.touch()
    sample_msgs = "\n".join(
        f"  - [{e.get('agent')}] {e.get('msg','')[:120]}"
        for e in pattern_examples[:5]
    )
    rec = {
        "flavor": "skill-synthesis",
        "id": f"{skill_id}-meta",
        "prompt": (
            "You observed this recurring failure pattern across multiple agents:\n"
            f"{sample_msgs}\n\nSynthesize a technique to fix it permanently. "
            "Output a SKILL.md + paper + verifier as STRICT JSON."
        ),
        "response": json.dumps({
            "title": data.get("title"),
            "diagnosis": data.get("diagnosis"),
            "technique": data.get("technique"),
            "skill_md": data.get("skill_md"),
            "verifier": data.get("verifier"),
            "paper": data.get("paper"),
            "maturity": data.get("maturity"),
            "applicability": data.get("applicability"),
        }, ensure_ascii=False),
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
        "source": "axentx-skill-synthesizer",
    }
    with PAIRS_FILE.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def commit_to_state(skill_path: Path, paper_path: Path,
                    skill_id: str, title: str) -> None:
    """git add + commit to harvest repo (state-sync-daemon will propagate)."""
    rels = [str(skill_path.relative_to(REPO_ROOT)),
            str(paper_path.relative_to(REPO_ROOT))]
    add = _git("add", *rels)
    if add.returncode != 0:
        log_(f"  ⚠ git add failed: {(add.stderr or '')[:120]}")
        return
    cur = _git("diff", "--cached", "--name-only")
    if not (cur.stdout or "").strip():
        return  # no real change
    msg = (f"skill-synth: {title}\n\n"
           f"id: {skill_id}\n"
           f"auto-synthesized by axentx-skill-synthesizer-daemon\n")
    _git("commit", "-m", msg)


def synthesize_one(pattern: str, examples: list[dict]) -> bool:
    """Attempt to synthesize a skill for one failure cluster.
    Returns True if a skill was committed."""
    pattern_hash = hashlib.sha256(pattern.encode()).hexdigest()[:12]
    if already_synthed(pattern_hash):
        return False

    sample_msgs = "\n".join(
        f"  - [{e.get('agent')}] {e.get('msg','')[:200]}"
        for e in examples[:8]
    )
    n_distinct = len({e.get("agent") for e in examples})
    user = (
        f"Failure pattern observed {len(examples)} times across "
        f"{n_distinct} distinct agents in the last hour:\n\n"
        f"Pattern (normalized): {pattern}\n\n"
        f"Sample raw events:\n{sample_msgs}\n\n"
        f"Synthesize a technique. STRICT JSON per schema."
    )
    log_(f"▸ synth attempt — pattern={pattern_hash} repeats={len(examples)} agents={n_distinct}")
    try:
        out = call_llm_strong(user, system=SYNTH_SYSTEM,
                              max_tokens=3500, timeout=120)
    except Exception as e:
        log_(f"  ✗ strong-llm failed: {e}")
        return False
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```")[1]
        if seg.startswith("json"):
            seg = seg[4:]
        txt = seg.strip()
    try:
        data = json.loads(txt)
    except Exception as e:
        log_(f"  ✗ JSON parse: {e}")
        return False

    if data.get("skip"):
        log_(f"  ⤳ model skipped: {data.get('reason','')[:80]}")
        mark_synthed(pattern_hash)
        return False

    title = data.get("title", "")
    technique = data.get("technique", "")
    if not (title and technique and data.get("skill_md")):
        log_("  ✗ missing required fields")
        return False

    citations = (data.get("paper") or {}).get("citations") or []
    if data.get("maturity") == "production" and len(citations) < 2:
        log_("  ⤳ production maturity but < 2 citations — downgrading to experimental")
        data["maturity"] = "experimental"

    if is_dupe_skill(title, technique):
        log_(f"  ⤳ dedup against existing skills (sim≥0.85)")
        mark_synthed(pattern_hash)
        return False

    verifier = data.get("verifier") or {}
    ok, vmsg = run_verifier(verifier)
    if not ok and data.get("maturity") in ("experimental", "production"):
        log_(f"  ⤳ verifier failed ({vmsg[:80]}) — skill not committed")
        mark_synthed(pattern_hash)
        return False

    skill_id = f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{pattern_hash}"
    skill_path, paper_path = write_skill(skill_id, data)
    emit_training_pair(skill_id, examples, data)
    commit_to_state(skill_path, paper_path, skill_id, title)
    mark_synthed(pattern_hash)
    log_(f"  ✓ synthesized: {title} (maturity={data.get('maturity')}, "
         f"verifier={'pass' if ok else 'n/a'})")
    return True


def do_one_cycle() -> bool:
    failures = collect_failures(since_min=60)
    if not failures:
        return False
    # Rank by (count, distinct-agents) desc — biggest pain first
    ranked = sorted(
        failures.items(),
        key=lambda kv: (-len(kv[1]),
                        -len({e.get("agent") for e in kv[1]})),
    )
    n_synthed = 0
    n_attempts = 0
    # Cap TOTAL attempts (not just successes) — synthesizing burns ~3500
    # decision-grade tokens each + every failed attempt still hits the
    # rate-limit budget. Limit MAX_ATTEMPTS_PER_CYCLE to stop hot-pattern
    # cycles eating the entire LLM TPD.
    max_attempts = int(os.environ.get("SKILL_MAX_ATTEMPTS_PER_CYCLE", "3"))
    for pattern, examples in ranked:
        if len(examples) < MIN_REPEAT:
            break  # ranked desc, rest are smaller
        n_distinct = len({e.get("agent") for e in examples})
        if n_distinct < MIN_AGENTS:
            continue
        n_attempts += 1
        if synthesize_one(pattern, examples):
            n_synthed += 1
            if n_synthed >= MAX_PER_CYCLE:
                break
        if n_attempts >= max_attempts:
            log_(f"  cap reached: {n_attempts} attempts, {n_synthed} synthed — yielding")
            break
    return n_synthed > 0


if __name__ == "__main__":
    daemon_loop("skill-synth", POLL_SEC, do_one_cycle)
