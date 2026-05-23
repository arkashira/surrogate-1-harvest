#!/usr/bin/env python3
"""axentx Tagline Improver — LLM-rewrites generic taglines for products.

Reads D1 bd.portfolio. For each product whose tagline matches the GENERIC
fallback pattern ("<Name> — built by an autonomous AI team..." or "<Name> —
production-grade..."), regenerates a punchy 80-char tagline via LLM.

Skips products whose taglines are already specific (from KNOWN map in
portfolio-categorize). Only regenerates if generic tail detected.

Cycle: 6h. Throttled: max 4 LLM calls per cycle to keep cost minimal.
"""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, call_llm  # noqa: E402

CYCLE_SEC = int(os.environ.get("TAGLINE_CYCLE_SEC", "21600"))  # 6h
MAX_PER_CYCLE = int(os.environ.get("TAGLINE_MAX_PER_CYCLE", "4"))
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"
DB = "ae95ac58-7b7e-40d9-8708-518c23281ae6"

# Generic fallback patterns (from portfolio-categorize.derive_for_unknown)
GENERIC_TAGLINES = [
    r"built by an autonomous AI team for teams that ship",
    r"production-grade AI infrastructure for engineering teams",
    r"keep cloud spend under control without spreadsheets",
    r"continuous compliance evidence for fast-moving teams",
    r"security signal you can act on, not noise",
    r"ship faster, rollback safer, sleep through alerts",
    r"modern billing \+ reconciliation for global teams",
    r"keep your data layer healthy without the on-call pages",
]


def is_generic(tagline: str) -> bool:
    return any(re.search(p, tagline, re.IGNORECASE) for p in GENERIC_TAGLINES)


PROMPT = """Write a single-sentence tagline for this developer-tools SaaS.

PRODUCT NAME: {name}
CATEGORY: {category}
HINT (slug parts): {slug_words}

Rules:
- Max 90 chars.
- Specific & concrete — avoid "platform", "solution", "tool" alone.
- No marketing-speak ("revolutionary", "AI-powered", "next-gen", "world-class").
- No exclamation marks, no emojis.
- Active voice, present tense.
- Should hint at the SPECIFIC pain it solves.

Output ONLY the tagline, no preamble or quotes."""


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("tagline-llm", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def cf_token():
    r = subprocess.run(
        ["bash", "-c",
         "grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env"
         " | cut -d= -f2-"],
        capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def d1_query(token, sql, params=None):
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/d1/database/{DB}/query",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def parse_entry(desc):
    """Extract category + tagline + tail from a portfolio entry."""
    m = re.match(r"^\s*\[CATEGORY:\s*([\w-]+)\]\s*(.+?)(?:\s*·\s*(.+))?$",
                 desc, re.DOTALL)
    if not m:
        return None
    return {
        "category": m.group(1),
        "tagline": m.group(2).strip(),
        "tail": "· " + m.group(3) if m.group(3) else "",
    }


def main():
    log("tagline-llm",
        f"start — cycle={CYCLE_SEC}s, max/cycle={MAX_PER_CYCLE}")
    while not _stop:
        try:
            token = cf_token()
            if not token:
                log("tagline-llm", "✗ no CF token")
            else:
                d = d1_query(token, "SELECT v FROM kv_store WHERE k=?",
                              ["bd.portfolio"])
                portfolio = json.loads(
                    d["result"][0]["results"][0]["v"])
                products = portfolio.get("products", {})

                changed = 0
                attempted = 0
                for slug, desc in list(products.items()):
                    if _stop:
                        break
                    if slug.startswith("PENDING-"):
                        continue
                    parsed = parse_entry(desc)
                    if not parsed:
                        continue
                    if not is_generic(parsed["tagline"]):
                        continue
                    if attempted >= MAX_PER_CYCLE:
                        break
                    attempted += 1

                    name = slug.replace("-", " ").replace("_", " ").title()
                    slug_words = " ".join(slug.replace("-", " ")
                                              .replace("_", " ").split())
                    try:
                        new_tag = call_llm(
                            PROMPT.format(name=name,
                                          category=parsed["category"],
                                          slug_words=slug_words),
                            system="You write tight tech-product taglines.",
                            max_tokens=80, timeout=45)
                    except Exception as e:
                        log("tagline-llm",
                            f"  ⚠ LLM {slug}: {type(e).__name__}")
                        continue
                    if not new_tag:
                        continue
                    new_tag = new_tag.strip().strip('"\'').splitlines()[0]
                    new_tag = re.sub(r"^[Tt]agline:\s*", "", new_tag)
                    new_tag = re.sub(r"\s+", " ", new_tag)[:140]
                    if not new_tag or len(new_tag) < 25:
                        continue
                    if is_generic(new_tag):
                        # LLM gave back another generic — skip
                        continue

                    new_desc = (
                        f"[CATEGORY: {parsed['category']}] {new_tag} "
                        f"{parsed['tail']}").strip()
                    new_desc = re.sub(r"\s+", " ", new_desc).strip()
                    products[slug] = new_desc
                    changed += 1
                    log("tagline-llm",
                        f"  ✓ {slug}: '{new_tag[:70]}'")

                if changed:
                    portfolio["products"] = products
                    payload = json.dumps(portfolio, ensure_ascii=False)
                    d1_query(token,
                             "INSERT OR REPLACE INTO kv_store (k,v,ts) "
                             "VALUES (?,?,?)",
                             ["bd.portfolio", payload, int(time.time())])
                    log("tagline-llm",
                        f"✓ wrote {changed} new taglines back to D1")
                else:
                    log("tagline-llm",
                        f"⊘ no eligible taglines (attempted={attempted})")
        except Exception as e:
            log("tagline-llm",
                f"⚠ cycle: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
