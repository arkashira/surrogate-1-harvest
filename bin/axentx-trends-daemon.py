#!/usr/bin/env python3
"""axentx trends — weekly market-signal scraper.

Pulls trending sources every TRENDS_POLL_SEC (default 6h):
  - github.com/trending (HTML scrape + LLM extract)
  - HN /show + /front of last week
  - dev.to /top
  - ProductHunt RSS

For each trending item, extracts: name, one-liner, what problem it claims
to solve, what's hot about it. Pushes as a research-queue item (so BD →
design → ... pipeline picks it up just like a Reddit pain).
This is OPPORTUNITY discovery (vs research-daemon's PAIN discovery)."""
from __future__ import annotations
import datetime, hashlib, json, os, sys, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, write_item, daemon_loop)
# 5 min poll — was 6h. Trending RSS / GH-stargazer pulls are cheap and the
# source services rate-limit us anyway. Faster cycle = faster market-signal
# feed-through. User directive 2026-05-02 round 3.
POLL_SEC = int(os.environ.get("TRENDS_POLL_SEC", "300"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CURSOR_FILE = REPO_ROOT / "state" / ".trends-cursor.json"

TRENDS_SYSTEM = """You scan a list of trending tech items. For each, output one
JSON object on a single line (NDJSON) extracting market signal:

{"name":"...","one_liner":"<what it does>","problem":"<what pain it addresses>","emerging_signal":"<why it's hot now>","relevance_to_axentx":"high|med|low|none","why":"<1 line>"}

ONLY include items with relevance high or med. Skip games, hardware,
crypto, consumer apps. Focus on dev tools, SaaS infra, AI, observability,
security, automation, productivity. Output is NDJSON (one JSON per line)."""

def fetch_gh_trending() -> list[dict]:
    """github.com/trending HTML — extract repo name + description."""
    req = urllib.request.Request("https://github.com/trending?since=weekly",
                                  headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    # crude regex parse — articles with h2 links
    import re
    repos = []
    pattern = re.compile(
        r'<h2 class="h3 lh-condensed">\s*<a[^>]*href="(/[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL)
    for m in pattern.finditer(html):
        url = "https://github.com" + m.group(1).strip()
        name = m.group(1).strip().lstrip("/")
        # find next paragraph after this h2 (description)
        seg = html[m.end():m.end()+2000]
        dm = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', seg, re.DOTALL)
        desc = ""
        if dm:
            desc = re.sub(r'<[^>]+>', '', dm.group(1)).strip()[:300]
        repos.append({"name": name, "url": url, "desc": desc})
    return repos[:25]

def fetch_ph_rss() -> list[dict]:
    """ProductHunt — RSS feed of today's products."""
    import xml.etree.ElementTree as ET
    req = urllib.request.Request("https://www.producthunt.com/feed",
                                  headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []
    out = []
    for item in root.findall(".//item")[:20]:
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title: continue
        out.append({"name": title, "url": link, "desc": desc[:300]})
    return out

def load_seen():
    try: return set(json.loads(CURSOR_FILE.read_text()).get("seen",[]))
    except: return set()
def save_seen(s):
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps({"seen": sorted(s)[-3000:]}))

def do_one() -> bool:
    seen = load_seen()
    fired = 0
    for src_name, fetcher in [("github-trending", fetch_gh_trending),
                              ("producthunt", fetch_ph_rss)]:
        log("trends", f"▸ {src_name}")
        try: items = fetcher()
        except Exception as e:
            log("trends", f"  ✗ {src_name}: {e}"); continue
        if not items: continue
        # batch into one LLM call (cheaper)
        bundle = "\n".join(
            f"- {it['name']}: {it.get('desc','')[:200]}" for it in items[:15]
        )
        try:
            out = call_llm(
                f"Trending source: {src_name}\nItems:\n{bundle}\n\n"
                f"Output NDJSON of the relevant items.",
                system=TRENDS_SYSTEM, max_tokens=2000, timeout=60)
        except Exception as e:
            log("trends", f"  ⚠ llm fail: {e}"); continue
        # parse NDJSON
        for line in out.splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")): continue
            try: d = json.loads(line)
            except: continue
            if d.get("relevance_to_axentx") not in ("high", "med"): continue
            fp = hashlib.sha256(f"{src_name}|{d.get('name','')}".encode()).hexdigest()[:16]
            if fp in seen: continue
            seen.add(fp)
            ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            it_id = f"{ts}-trend-{fp}"
            item = {
                "id": it_id, "stage": "research",
                "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                "post": {"source": src_name, "url": "", "title": d.get("name",""),
                         "body": json.dumps(d), "score": 0, "num_comments": 0},
                "verdict": {"is_real_pain": True,
                            "pain_one_liner": d.get("problem","?"),
                            "domain": "saas",
                            "severity": 7 if d.get("relevance_to_axentx")=="high" else 5,
                            "audience": "tech professionals",
                            "evidence": d.get("emerging_signal","")},
                "history": [{"stage":"research","actor":"axentx-trends",
                            "output": json.dumps(d, ensure_ascii=False),
                            "at": datetime.datetime.utcnow().isoformat()+"Z"}],
                "current": {"text": json.dumps(d, ensure_ascii=False)},
            }
            write_item(item, "bd")
            fired += 1
            log("trends", f"  ✓ trend ({d.get('relevance_to_axentx')}): {d.get('name','')[:50]}")
    save_seen(seen)
    log("trends", f"cycle done — {fired} trends → bd-queue")
    return fired > 0

if __name__ == "__main__":
    daemon_loop("trends", POLL_SEC, do_one)
