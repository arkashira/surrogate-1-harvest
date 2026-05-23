#!/usr/bin/env python3
"""axentx mega-startup-sources — consolidates 15+ startup-idea sources into
one daemon. Lower per-cycle volume per source = same total throughput
without spawning 15 separate processes.

Sources:
  RSS:
    - https://www.failory.com/feed                  (failure stories)
    - https://www.sideprojectors.com/feed.atom      (side projects for sale)
    - https://blog.acquire.com/feed                 (SaaS acquisition stories)
    - https://www.tinyacquisitions.com/feed         (sub-$50K SaaS deals)
    - https://www.smallbets.co/feed                 (Daniel Vassallo community)
    - https://wip.co/rss                            (build-in-public log)
    - https://nocodefounders.com/feed               (no-code SaaS)
    - https://www.notboring.co/feed                 (Packy McCormick)
    - https://saashub.com/blog/feed                 (SaaS reviews + alts)
    - https://getlatka.com/saas/feed                (SaaS revenue interviews)
    - https://www.starterstory.com/feed             (Pat Walls case studies)
    - https://www.indiehackers.com/posts.rss        (already in ih-stream
                                                     but we use TOP filter
                                                     here for revenue-tagged)
    - https://blog.smallbets.co/rss                 (small-bet community)
  Reddit (covered via reddit-stream — adding subs):
    - r/SaaS, r/microsaas, r/sideproject,
      r/Entrepreneur, r/EntrepreneurRideAlong,
      r/startups, r/SaaSdotcom
"""
import datetime, hashlib, json, os, re, signal, sys, time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,
                             new_trace_id)

POLL_SEC = int(os.environ.get("MEGA_POLL_SEC", "5400"))   # 90min
SEEN_FILE = REPO_ROOT / "state" / "mega-startup-sources.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; axentx-discovery/1.0)"

FEEDS = [
    "https://www.failory.com/feed",
    "https://www.sideprojectors.com/feed.atom",
    "https://blog.acquire.com/feed",
    "https://www.tinyacquisitions.com/feed",
    "https://wip.co/rss",
    "https://nocodefounders.com/feed",
    "https://saashub.com/blog/feed",
    "https://www.starterstory.com/feed",
    "https://www.smallbets.co/feed",
    "https://blog.smallbets.co/rss",
    "https://www.signupless.com/rss",
    "https://www.coderscat.com/feed.xml",
    "https://www.allthingsadmin.com/blog/feed",
    "https://thehustle.co/feed",
    "https://www.failedstartup.com/feed",
]

EXTRACT_SYSTEM = (
    "You are reading a startup/SaaS/indie-hacker article. Extract concrete "
    "product idea + monetization. Reject if no $$$ path."
)
EXTRACT_PROMPT = """Article:
{a}

Output STRICT JSON:
{{
  "idea": "1-sentence axentx-spawnable product",
  "audience": "specific buyer",
  "monetization_signal": "low|medium|high",
  "pricing": "$X/mo guess",
  "tam_signal": "low|medium|high"
}}
"""

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _http(url, t=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=t) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse(xml):
    items = []
    for m in re.finditer(r"<(?:item|entry)>(.*?)</(?:item|entry)>",
                         xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(
            r"<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
            chunk, re.DOTALL)
        link = re.search(r"<link[^>]*href=\"([^\"]+)\"", chunk) or \
               re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(
            r"<(?:description|summary)[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:description|summary)>",
            chunk, re.DOTALL)
        if not (title and link):
            continue
        items.append({
            "title": re.sub(r"<[^>]+>", "", title.group(1)).strip()[:200],
            "url": link.group(1).strip(),
            "snippet": (re.sub(r"<[^>]+>", " ", desc.group(1))
                        if desc else "")[:1500],
        })
    return items


def _emit(art, sigs, source):
    h = hashlib.sha1(art["url"].encode()).hexdigest()[:14]
    item_id = (f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
               f"mega-{h}")
    write_item({
        "id": item_id, "trace_id": new_trace_id(), "discovery_id": item_id,
        "stage": "validator", "source": f"mega/{source}",
        "url": art["url"], "title": art["title"],
        "pain_one_liner": sigs.get("idea", "")[:240],
        "audience": sigs.get("audience", ""),
        "monetization_signal": sigs.get("monetization_signal", "low"),
        "pricing_signal": sigs.get("pricing", ""),
        "tam_signal": sigs.get("tam_signal", "low"),
        "axentx_idea": sigs.get("idea") or "",
        "raw_signals": sigs,
        "authority_score": 0.7,
        "history": [{
            "stage": "research", "actor": "mega-startup-sources",
            "output": f"mega/{source}: {art['title'][:80]}",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }, "validator")


def do_one():
    try:
        seen = set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        seen = set()
    n = e = 0
    for feed in FEEDS:
        if _stop: break
        src = feed.split("//")[1].split("/")[0]
        for art in _parse(_http(feed))[:3]:   # 3 per source per cycle
            if _stop: break
            h = hashlib.sha1(art["url"].encode()).hexdigest()
            if h in seen: continue
            seen.add(h)
            n += 1
            try:
                out = call_llm(
                    EXTRACT_PROMPT.format(
                        a=f"Title: {art['title']}\nURL: {art['url']}\n\n"
                          f"Snippet: {art.get('snippet','')[:1200]}"),
                    system=EXTRACT_SYSTEM, max_tokens=300, timeout=25)
            except Exception:
                continue
            txt = out.strip()
            if "```" in txt:
                seg = txt.split("```", 2)
                if len(seg) >= 2:
                    txt = seg[1]
                    if txt.startswith("json"):
                        txt = txt[4:]
                    txt = txt.strip()
            try:
                sigs = json.loads(txt)
            except Exception:
                continue
            mon = (sigs.get("monetization_signal") or "").lower()
            if mon not in ("medium", "high"): continue
            _emit(art, sigs, src)
            e += 1
            time.sleep(1)
        time.sleep(2)
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))
    except Exception:
        pass
    log("mega-startup-sources",
        f"cycle: {n} new from {len(FEEDS)} sources, emitted {e}")
    return n > 0


if __name__ == "__main__":
    daemon_loop("mega-startup-sources", POLL_SEC, do_one)
