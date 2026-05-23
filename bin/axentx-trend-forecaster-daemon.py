#!/usr/bin/env python3
"""axentx trend-forecaster — detect rising trends + forecast 3-6mo opportunities.

User directive 2026-05-05:
  > "มี agent ที่เก็บสถิติ และ forecast trend ตลาดด้วย ว่าในอนาคตเรื่อง
  >  ไหนกำลังจะมา แล้วมีโอกาสทำเงินยังไง ให้เราสร้างเป็นหัวแถวได้ก่อน"

Cycle (every 4h, leader):
  1. Read last 14d pain items from research-queue + done/.
  2. Extract keywords per item (n-grams 1-3 words).
  3. Compute frequency over time:
       - bucket by day for last 14d
       - compute slope: rising = (last 3d freq) > 1.5× (prior 11d avg)
  4. For top-20 rising keywords:
       a. LLM forecast: "Is this a real 3-6mo trend or noise? Why?"
       b. LLM business-angle: "How does axentx make money on this?"
       c. Score: confidence (0-100) × revenue_potential (0-100)
  5. Write to shared_kv:
       - "trend.rising_keywords" = [top 10 keyword strings]
       - "trend.forecasts" = [{keyword, confidence, revenue, angle}]
  6. social-listener picks seeds from "trend.rising_keywords"
     product-synth biases toward "trend.forecasts[].angle"

Result: pipeline focuses on what's GROWING, not what's already saturated.
"""
from __future__ import annotations

import collections
import datetime
import json
import os
import re
import signal
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop, call_llm  # noqa: E402

POLL_SEC = int(os.environ.get("FORECASTER_POLL_SEC", "14400"))   # 4 h
HOST = socket.gethostname()
SHARED_QUEUES = Path(os.environ.get(
    "SHARED_QUEUES",
    "/opt/surrogate-1-harvest/state/swarm-shared"))
WINDOW_DAYS = int(os.environ.get("FORECASTER_WINDOW_DAYS", "14"))
TOP_N_KEYWORDS = int(os.environ.get("FORECASTER_TOP_N", "20"))

FORECAST_SYSTEM = (
    "You are a market-trend forecaster. Given a rising keyword + frequency "
    "data, judge: (1) is this a real 3-6mo trend or noise? (2) what kind of "
    "business angle could axentx pursue?\n\n"
    "Output STRICT JSON:\n"
    "{\n"
    '  "is_real_trend": true|false,\n'
    '  "confidence": 0-100,\n'
    '  "horizon_months": 3|6|12,\n'
    '  "revenue_potential": 0-100,\n'
    '  "business_angle": "1-line — how axentx makes money on this",\n'
    '  "blue_ocean_th": true|false,\n'
    '  "first_mover_window_days": <int — how soon must we ship>,\n'
    '  "reason_kill": "<reason if NOT a real trend>"\n'
    "}\n\n"
    "Bias: be skeptical of fad-y / pump-and-dump trends. Prefer durable "
    "B2B niches with clear paying customers. Reject noise (one-week spikes, "
    "social-media drama, news cycles).\n"
)


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


_STOP_WORDS = {
    "the","a","an","and","or","of","for","to","in","on","with","by","is",
    "are","be","that","this","it","as","at","from","i","you","we","they",
    "what","how","why","when","do","does","did","not","no","my","your",
    "but","if","so","just","can","cant","can't","cannot","get","got","had",
}


def _ngrams(text: str, n_min: int = 1, n_max: int = 3) -> list[str]:
    """Extract n-grams (1-3 words). Lowercase, no stopwords."""
    words = re.findall(r"[A-Za-z฀-๿]{4,18}", text.lower())
    words = [w for w in words if w not in _STOP_WORDS]
    grams = set()
    for i in range(len(words)):
        for n in range(n_min, n_max + 1):
            if i + n <= len(words):
                grams.add(" ".join(words[i:i+n]))
    return list(grams)


def _scan_items() -> list[tuple[str, datetime.datetime]]:
    """Scan research/done queues. Returns [(text, mtime_dt), ...]."""
    items: list[tuple[str, datetime.datetime]] = []
    for qname in ("research-queue", "done", "validator-queue", "bd-queue"):
        qdir = SHARED_QUEUES / qname
        if not qdir.exists(): continue
        cutoff_ts = (datetime.datetime.utcnow() -
                     datetime.timedelta(days=WINDOW_DAYS)).timestamp()
        for fp in qdir.glob("*.json"):
            try:
                mt = fp.stat().st_mtime
                if mt < cutoff_ts: continue
                d = json.loads(fp.read_text())
                text = (d.get("current") or {}).get("text", "")
                title = (d.get("post") or {}).get("title", "")
                items.append((f"{title} {text}"[:1500],
                              datetime.datetime.fromtimestamp(mt)))
            except Exception:
                continue
    return items


def _compute_rising(items: list[tuple[str, datetime.datetime]]
                    ) -> list[tuple[str, float, int, int]]:
    """Returns sorted [(keyword, growth_ratio, recent_count, prior_count), ...]
    Growth = recent (last 3d) freq / prior (4-14d) avg.
    """
    now = datetime.datetime.utcnow()
    recent_cutoff = now - datetime.timedelta(days=3)
    prior_cutoff = now - datetime.timedelta(days=WINDOW_DAYS)

    recent_freq: dict[str, int] = collections.Counter()
    prior_freq: dict[str, int] = collections.Counter()
    for text, ts in items:
        for g in _ngrams(text):
            if ts >= recent_cutoff:
                recent_freq[g] += 1
            elif ts >= prior_cutoff:
                prior_freq[g] += 1

    rising = []
    prior_total_days = max(1, WINDOW_DAYS - 3)
    for kw, recent_n in recent_freq.items():
        if recent_n < 3: continue   # noise floor
        prior_n = prior_freq.get(kw, 0)
        prior_avg = (prior_n / prior_total_days) * 3   # normalize to 3d
        ratio = recent_n / max(prior_avg, 1.0)
        if ratio < 1.5: continue
        rising.append((kw, ratio, recent_n, prior_n))
    rising.sort(key=lambda x: x[1], reverse=True)
    return rising[:TOP_N_KEYWORDS]


def _parse_json(out: str) -> dict | None:
    txt = out.strip()
    if "```" in txt:
        for c in txt.split("```"):
            if "{" in c: txt = c.lstrip("json").strip(); break
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    try: return json.loads(txt)
    except Exception: return None


def forecast_keyword(kw: str, ratio: float, recent: int, prior: int
                     ) -> dict | None:
    prompt = (
        f"# Rising keyword: {kw!r}\n"
        f"# Stats:\n"
        f"  - last 3d frequency: {recent}\n"
        f"  - prior 11d frequency: {prior} "
        f"(growth ratio: {ratio:.1f}x)\n\n"
        f"# Task: forecast trend + business angle. STRICT JSON.")
    try:
        out = call_llm(prompt, system=FORECAST_SYSTEM,
                       max_tokens=400, timeout=30)
    except Exception:
        return None
    return _parse_json(out)


def cycle():
    if _stop: return False
    if not _is_leader():
        log("trend-forecaster", "  ⤷ not leader — skip")
        return False

    items = _scan_items()
    log("trend-forecaster",
        f"  ▸ scanned {len(items)} items in last {WINDOW_DAYS}d")
    if len(items) < 20:
        log("trend-forecaster", "  ⊘ too few items, skip")
        return False

    rising = _compute_rising(items)
    log("trend-forecaster",
        f"  ▸ {len(rising)} rising keywords")
    if not rising:
        return False

    forecasts: list[dict] = []
    rising_keywords: list[str] = []
    for kw, ratio, recent, prior in rising[:10]:
        f = forecast_keyword(kw, ratio, recent, prior)
        if not f: continue
        f["keyword"] = kw
        f["recent_freq"] = recent
        f["prior_freq"] = prior
        f["growth_ratio"] = round(ratio, 2)
        forecasts.append(f)
        if f.get("is_real_trend") and f.get("confidence", 0) >= 40:
            rising_keywords.append(kw)
            log("trend-forecaster",
                f"  ✓ TREND: {kw!r} conf={f.get('confidence')} "
                f"rev={f.get('revenue_potential')} "
                f"angle={f.get('business_angle','-')[:60]}")

    # Persist for social-listener + product-synth to consume
    try:
        from axentx_shared import kv_set
        kv_set("trend.rising_keywords", rising_keywords[:15])
        kv_set("trend.forecasts", forecasts[:20])
    except Exception:
        pass

    log("trend-forecaster",
        f"  ✓ published {len(rising_keywords)} confirmed trends + "
        f"{len(forecasts)} forecasts")
    return False


if __name__ == "__main__":
    daemon_loop("trend-forecaster", POLL_SEC, cycle)
