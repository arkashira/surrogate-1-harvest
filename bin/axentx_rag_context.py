"""axentx_rag_context — query past pain/product RAG snippets at decision time.

Used by pitch / bd / product-synth to ground their LLM calls on REAL past
data (what worked, what failed, similar competitors found).

Strategy:
  1. shared_kv["rag.snippets.<keyword>"] is populated by hf-rag-loader after
     each cycle (it indexes pulled rows by salient keyword).
  2. This module reads them at LLM-call time — adds ~5 best-matched snippets
     to the system prompt so personas/bd/synth see "what we know".
  3. Falls back to FS cache if Supabase 522.

Usage:
    from axentx_rag_context import attach_rag
    system_with_rag = attach_rag(system, hypothesis_text, max_snippets=5)
    out = call_llm(prompt, system=system_with_rag)
"""
from __future__ import annotations

import os
import re
import json
from pathlib import Path

_RAG_CACHE_DIR = Path(os.environ.get(
    "RAG_CACHE_DIR",
    "/opt/surrogate-1-harvest/state/rag-cache"))


def _stop_words() -> set[str]:
    return {"the","a","an","and","or","of","for","to","in","on","with","by",
            "is","are","be","of","that","this","it","as","at","from","i",
            "you","we","they","what","how","why","when"}


def _keywords(text: str, top_n: int = 8) -> list[str]:
    """Extract salient lowercase keywords (length ≥ 4)."""
    words = re.findall(r"[A-Za-z]{4,20}", text.lower())
    sw = _stop_words()
    freq: dict[str, int] = {}
    for w in words:
        if w in sw: continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_n]]


def _read_kv(key: str):
    try:
        # Use axentx_shared which now has FS-cache fallback
        from axentx_shared import kv_get
        return kv_get(key)
    except Exception:
        return None


def _read_fs_index(keyword: str) -> list[str]:
    """Read FS-cached snippets for a keyword. Returns list of snippet strings."""
    fp = _RAG_CACHE_DIR / f"by-keyword/{keyword[:20]}.jsonl"
    if not fp.exists():
        return []
    out = []
    try:
        with fp.open() as f:
            for line in f:
                if line.strip():
                    try:
                        d = json.loads(line)
                        text = (d.get("title", "") or "") + " — " + (d.get("body", "") or "")[:200]
                        out.append(text[:300])
                    except Exception:
                        continue
                if len(out) >= 5:
                    break
    except Exception:
        pass
    return out


def fetch_rag_snippets(query_text: str, max_snippets: int = 5) -> list[str]:
    """Find best matching past-pain/product snippets for a query."""
    keywords = _keywords(query_text)
    snippets: list[str] = []
    for kw in keywords:
        # Try shared_kv first (cross-host shared)
        kv_val = _read_kv(f"rag.snippets.{kw}")
        if isinstance(kv_val, list):
            for s in kv_val:
                if isinstance(s, str) and s not in snippets:
                    snippets.append(s)
                    if len(snippets) >= max_snippets:
                        return snippets
        # Then FS cache
        for s in _read_fs_index(kw):
            if s not in snippets:
                snippets.append(s)
                if len(snippets) >= max_snippets:
                    return snippets
    return snippets


def attach_rag(system: str, query_text: str, max_snippets: int = 5,
               header: str = "## Past data (RAG)") -> str:
    """Append RAG snippets to a system prompt. Returns enriched prompt."""
    snips = fetch_rag_snippets(query_text, max_snippets)
    if not snips:
        return system
    block = f"\n\n{header}\n"
    for i, s in enumerate(snips, 1):
        block += f"  {i}. {s[:280]}\n"
    block += ("\nUse these data points when reasoning. If they show "
              "similar past products failed, factor that in.\n")
    return system + block


__all__ = ["fetch_rag_snippets", "attach_rag"]
