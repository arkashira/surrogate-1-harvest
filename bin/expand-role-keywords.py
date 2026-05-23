#!/usr/bin/env python3
"""
One-shot keyword expander — uses Cerebras (or fallback) to expand each
SDLC role's core/adjacent skills into 100+ specific HF dataset search
keywords. Output is written back to role-knowledge-map.json under a new
"expanded" key per role.

Idempotent — re-running just refreshes "expanded" keywords. Existing
core/adjacent are untouched.

Run from cron weekly (or manually). Discoverer auto-reads the map on
its next cycle and fires search queries for the expanded list.

Usage:  python expand-role-keywords.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROLE_MAP_PATH = Path.home() / ".surrogate/agents/role-knowledge-map.json"

PROVIDERS = [
    {
        "name": "cerebras",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "qwen-3-235b-a22b-instruct-2507",
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "tencent/hy3-preview:free",
    },
]


def call_llm(prompt: str, timeout: int = 90) -> str | None:
    for p in PROVIDERS:
        key = os.environ.get(p["key_env"], "").strip()
        if not key:
            continue
        body = json.dumps({
            "model": p["model"],
            "messages": [
                {"role": "system",
                 "content": "You are a senior tech recruiter who reads thousands of job descriptions. Output clean comma-separated keyword lists, no prose."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1500,
            "temperature": 0.4,
        }).encode()
        req = urllib.request.Request(
            p["url"],
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 surrogate-1/expand-keywords",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if content:
                    print(f"  [{p['name']}] ok ({len(content)} chars)", flush=True)
                    return content
        except Exception as e:
            print(f"  [{p['name']}] err: {type(e).__name__}: {str(e)[:80]}", flush=True)
            continue
    return None


def expand_role(role_name: str, role_def: dict) -> list[str]:
    core = role_def.get("core", [])
    adjacent = role_def.get("adjacent", [])
    prompt = f"""Role: {role_name}

Existing core skills: {', '.join(core)}
Adjacent skills: {', '.join(adjacent)}

Task: Output exactly 80 highly specific keyword phrases (3-6 words each) that this role's job description would mention. Focus on:
- specific frameworks, tools, libraries by name
- concrete certifications and standards (CKA, AWS SAA, ISO 27001, etc.)
- specific design patterns and methodologies
- production-grade vocabulary used by senior engineers
- emerging 2025-2026 tech in this domain

Output: comma-separated list. NO numbering. NO categories. NO explanatory text. Just keywords."""

    response = call_llm(prompt)
    if not response:
        return []

    # Parse comma-separated keywords, strip noise
    kws = []
    for piece in response.replace(";", ",").split(","):
        kw = piece.strip().strip(".\"'`*-•").strip()
        # remove leading numbers like "1. " or "1) "
        if kw and kw[0].isdigit():
            for sep in (". ", ") ", "- "):
                if sep in kw[:5]:
                    kw = kw.split(sep, 1)[1].strip()
                    break
        if 3 <= len(kw) <= 80 and any(c.isalpha() for c in kw):
            kws.append(kw.lower())

    # Dedup keep order
    seen = set()
    deduped = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped[:80]


def main():
    if not ROLE_MAP_PATH.exists():
        sys.exit(f"role-knowledge-map.json not found at {ROLE_MAP_PATH}")

    data = json.loads(ROLE_MAP_PATH.read_text())
    roles = data.get("roles", {})
    if not roles:
        sys.exit("no roles in map")

    total_added = 0
    for name, role_def in roles.items():
        existing = len(role_def.get("expanded", []))
        print(f"\n▶ {name} (existing core={len(role_def.get('core',[]))} adjacent={len(role_def.get('adjacent',[]))} expanded={existing})", flush=True)
        new_kws = expand_role(name, role_def)
        if not new_kws:
            print(f"  (no expansion — all providers failed)", flush=True)
            continue
        # Merge with any existing expanded keywords
        existing_set = set(role_def.get("expanded", []))
        merged = list(existing_set | set(new_kws))
        role_def["expanded"] = sorted(merged)
        added = len(role_def["expanded"]) - existing
        total_added += added
        print(f"  +{added} keywords (total expanded={len(role_def['expanded'])})", flush=True)
        time.sleep(2)  # gentle rate-limit between roles

    # Write back
    ROLE_MAP_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n✅ wrote {ROLE_MAP_PATH} — added {total_added} new keywords across {len(roles)} roles")


if __name__ == "__main__":
    main()
