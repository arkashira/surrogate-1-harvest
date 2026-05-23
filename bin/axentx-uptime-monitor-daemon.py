#!/usr/bin/env python3
"""axentx Uptime Monitor — health checks every live product URL + status page.

Reads /opt/surrogate-1-harvest/state/live-urls.json and probes each URL.
Records:
  /opt/surrogate-1-harvest/state/uptime.json
    {
      "last_check_ts": "...",
      "checks": {
        "<slug>": {"url", "status", "latency_ms", "ok",
                   "uptime_24h": float, "history": [{ts,status,latency},...]}
      },
      "summary": {"total": N, "up": M, "avg_latency_ms": ..., "uptime_pct_24h": ..}
    }

Cycle: 5 min. Keeps last 288 history points (24h × 12 per hour).

Status page (HTML) is regenerated each cycle and deployed to:
  https://status.axentx.pages.dev/   (Pages project `axentx-status`)
"""
from __future__ import annotations
import base64
import datetime
import html
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

try:
    import blake3 as _blake3
except ImportError:
    _blake3 = None

CYCLE_SEC = int(os.environ.get("UPTIME_CYCLE_SEC", "300"))
LIVE_URLS = REPO_ROOT / "state" / "live-urls.json"
UPTIME_FILE = REPO_ROOT / "state" / "uptime.json"
HISTORY_MAX = 288  # 24h at 5-min cadence
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"
PROJECT = "axentx-status"

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("uptime", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>axentx · status — {summary_line}</title>
<meta http-equiv="refresh" content="60">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg:#0a0e1a; --bg-2:#11172a; --card:#161b30;
  --fg:#e6e9f5; --muted:#8a91a8; --accent:#00e5ff;
  --good:#4ade80; --warn:#facc15; --bad:#f87171;
}}
body {{ background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif; line-height: 1.55; }}
.wrap {{ max-width: 980px; margin: 0 auto; padding: 0 24px; }}
header {{ padding: 60px 0 30px; border-bottom: 1px solid var(--bg-2); }}
.brand {{ font-size: 13px; color: var(--accent); letter-spacing: 0.18em;
  text-transform: uppercase; margin-bottom: 12px; }}
h1 {{ font-size: 38px; letter-spacing: -0.01em; margin-bottom: 12px; }}
.summary {{ color: var(--muted); font-size: 16px; }}
.summary strong {{ color: var(--fg); }}
.indicator {{
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--good); margin-right: 8px;
}}
.indicator.bad {{ background: var(--bad); }}
.indicator.warn {{ background: var(--warn); }}
section {{ padding: 30px 0; }}
.list {{ display: grid; gap: 10px; }}
.row {{
  background: var(--card); padding: 16px 18px; border-radius: 10px;
  display: grid; grid-template-columns: 1fr auto auto auto;
  gap: 16px; align-items: center; border: 1px solid var(--bg-2);
}}
.row .name {{ font-weight: 600; }}
.row .name a {{ color: var(--fg); text-decoration: none; }}
.row .name a:hover {{ color: var(--accent); }}
.row .status {{ font-size: 13px; }}
.row .latency {{ color: var(--muted); font-size: 13px; }}
.row .uptime {{ font-size: 13px; color: var(--muted); }}
.row.up {{ border-left: 3px solid var(--good); }}
.row.down {{ border-left: 3px solid var(--bad); }}
.row.slow {{ border-left: 3px solid var(--warn); }}
.bars {{ display: flex; gap: 2px; margin-top: 8px; height: 18px; }}
.bar {{ flex: 1; background: var(--good); border-radius: 1px;
  min-width: 2px; max-width: 6px; opacity: 0.9; }}
.bar.bad {{ background: var(--bad); }}
.bar.warn {{ background: var(--warn); }}
footer {{ padding: 40px 0; color: var(--muted); font-size: 13px; text-align: center; }}
</style></head>
<body>

<header><div class="wrap">
<div class="brand">axentx</div>
<h1>status</h1>
<p class="summary">
  <span class="indicator {summary_class}"></span>
  <strong>{summary_line}</strong>
  · avg latency <strong>{avg_latency}ms</strong>
  · 24h uptime <strong>{uptime_pct_24h}%</strong>
  · last checked {last_check}
</p>
</div></header>

<section><div class="wrap">
<div class="list">
{rows_html}
</div>
</div></section>

<footer><div class="wrap">
auto-refreshes every 60s · powered by axentx ·
<a style="color:var(--accent)" href="https://axentx.pages.dev/">products</a>
</div></footer>

</body></html>
"""


def _http(method, url, headers=None, data=None, timeout=60):
    h = {"User-Agent": "axentx-uptime/1"}
    if headers:
        h.update(headers)
    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _cf_token():
    r = subprocess.run(
        ["bash", "-c",
         "grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env"
         " | cut -d= -f2-"],
        capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def _ensure_project(token):
    s, b = _http(
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects",
        headers={"Authorization": f"Bearer {token}"},
        data={"name": PROJECT, "production_branch": "main"})
    if s == 200 or s == 409 or "already exists" in b.lower() or "duplicate" in b.lower():
        return True
    log("uptime", f"  ⚠ ensure project: {s} {b[:200]}")
    return False


def _deploy_html(token, html_text):
    if _blake3 is None:
        return None
    content = html_text.encode()
    h = _blake3.blake3(content + b"html").hexdigest()[:32]
    manifest = {"/index.html": h}
    s, b = _http(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/pages/projects/{PROJECT}/upload-token",
        headers={"Authorization": f"Bearer {token}"})
    d = json.loads(b)
    if not d.get("success"):
        return None
    jwt = d["result"]["jwt"]
    s, b = _http(
        "POST",
        "https://api.cloudflare.com/client/v4/pages/assets/check-missing",
        headers={"Authorization": f"Bearer {jwt}"},
        data={"hashes": [h]})
    d = json.loads(b)
    missing = d.get("result") if d.get("success") else None
    if missing is None:
        return None
    if missing:
        payload = [{
            "key": h,
            "value": base64.b64encode(content).decode(),
            "metadata": {"contentType": "text/html"},
            "base64": True,
        }]
        s, b = _http(
            "POST",
            "https://api.cloudflare.com/client/v4/pages/assets/upload",
            headers={"Authorization": f"Bearer {jwt}"}, data=payload, timeout=120)
        if not json.loads(b).get("success"):
            return None
    boundary = f"----axentxStatus{int(time.time())}"
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="manifest"\r\n\r\n',
        json.dumps(manifest).encode(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    s, b = _http(
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/pages/projects/{PROJECT}/deployments",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body, timeout=120)
    d = json.loads(b) if b else {}
    if d.get("success"):
        return d["result"].get("url")
    return None


def probe(url, timeout=8):
    """HEAD then GET fallback. Returns (status, latency_ms)."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="HEAD",
                                      headers={"User-Agent": "axentx-uptime/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        # Some servers reject HEAD; try GET
        if e.code in (405, 501):
            try:
                req = urllib.request.Request(url, method="GET",
                                              headers={"User-Agent": "axentx-uptime/1"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, int((time.time() - t0) * 1000)
            except Exception:
                return 0, int((time.time() - t0) * 1000)
        return e.code, int((time.time() - t0) * 1000)
    except Exception:
        return 0, int((time.time() - t0) * 1000)


def normalize_prod_url(url):
    """Strip deployment-hash subdomain prefix to get production URL."""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        parts = u.hostname.split(".")
        if (len(parts) == 4 and parts[-2] == "pages"
                and parts[-1] == "dev" and len(parts[0]) in (8, 32)):
            return f"https://{'.'.join(parts[1:])}/"
        return url.rstrip("/") + "/"
    except Exception:
        return url


def render_status(state):
    checks = state.get("checks", {})
    rows = []
    n_up = n_down = 0
    latencies = []
    uptime_24h_vals = []
    for slug in sorted(checks.keys()):
        c = checks[slug]
        ok = c.get("ok", False)
        status = c.get("status", 0)
        latency = c.get("latency_ms", 0)
        u24 = c.get("uptime_24h", 0)
        url = c.get("url", "#")
        history = c.get("history", [])
        if ok:
            n_up += 1
            cls = "up"
            indicator = "good"
        else:
            n_down += 1
            cls = "down"
            indicator = "bad"
        if latency > 3000:
            cls = "slow"
            indicator = "warn"
        latencies.append(latency)
        if u24 is not None:
            uptime_24h_vals.append(u24)
        bars_html = ""
        for h in history[-60:]:
            bcls = "" if h.get("ok") else "bad"
            if h.get("latency", 0) > 3000:
                bcls = "warn"
            bars_html += f'<div class="bar {bcls}"></div>'
        rows.append(
            f'<div class="row {cls}">'
            f'<div class="name">'
            f'<span class="indicator {indicator}"></span>'
            f'<a href="{html.escape(url)}" target="_blank" rel="noopener">'
            f'{html.escape(slug)}</a>'
            f'<div class="bars">{bars_html}</div></div>'
            f'<div class="status">{status if status else "—"}</div>'
            f'<div class="latency">{latency}ms</div>'
            f'<div class="uptime">{u24:.1f}%</div>'
            f'</div>'
        )
    total = n_up + n_down
    if total == 0:
        summary_line = "No products deployed yet"
        summary_class = "warn"
        avg_latency = 0
        uptime_pct_24h = "—"
    elif n_down == 0:
        summary_line = f"All {total} systems operational"
        summary_class = "good"
        avg_latency = sum(latencies) // total if latencies else 0
        uptime_pct_24h = f"{(sum(uptime_24h_vals)/len(uptime_24h_vals)):.2f}" if uptime_24h_vals else "100.00"
    else:
        summary_line = f"{n_down}/{total} systems degraded"
        summary_class = "bad"
        avg_latency = sum(latencies) // total if latencies else 0
        uptime_pct_24h = f"{(sum(uptime_24h_vals)/len(uptime_24h_vals)):.2f}" if uptime_24h_vals else "—"
    return STATUS_HTML.format(
        summary_line=summary_line,
        summary_class=summary_class,
        avg_latency=avg_latency,
        uptime_pct_24h=uptime_pct_24h,
        last_check=state.get("last_check_ts", "—"),
        rows_html="\n".join(rows) or "<div class='row'>building inventory…</div>",
    )


def main():
    log("uptime",
        f"start — cycle={CYCLE_SEC}s → status.axentx.pages.dev")
    if _blake3 is None:
        log("uptime", "✗ blake3 missing")
        return 1

    while not _stop:
        try:
            urls = {}
            if LIVE_URLS.is_file():
                try:
                    raw = json.loads(LIVE_URLS.read_text())
                    for slug, info in raw.items():
                        u = (info or {}).get("url")
                        if u:
                            urls[slug] = normalize_prod_url(u)
                except Exception:
                    urls = {}

            state = {}
            if UPTIME_FILE.is_file():
                try:
                    state = json.loads(UPTIME_FILE.read_text())
                except Exception:
                    state = {}
            checks = state.setdefault("checks", {})

            now_iso = datetime.datetime.utcnow().isoformat() + "Z"
            now_t = int(time.time())

            for slug, url in urls.items():
                status, latency = probe(url)
                ok = 200 <= status < 400
                rec = checks.setdefault(slug, {"history": []})
                rec["url"] = url
                rec["status"] = status
                rec["latency_ms"] = latency
                rec["ok"] = ok
                rec["last_check_ts"] = now_iso
                hist = rec.setdefault("history", [])
                hist.append({"ts": now_t, "status": status,
                             "latency": latency, "ok": ok})
                if len(hist) > HISTORY_MAX:
                    rec["history"] = hist[-HISTORY_MAX:]
                # 24h uptime
                cutoff = now_t - 86400
                recent = [h for h in rec["history"] if h["ts"] >= cutoff]
                if recent:
                    rec["uptime_24h"] = round(
                        sum(1 for h in recent if h["ok"]) / len(recent) * 100, 2)
                else:
                    rec["uptime_24h"] = 100.0 if ok else 0.0

            # Drop entries for slugs no longer in urls
            for slug in list(checks.keys()):
                if slug not in urls:
                    del checks[slug]

            state["last_check_ts"] = now_iso
            state["last_check_unix"] = now_t

            UPTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
            UPTIME_FILE.write_text(json.dumps(state, indent=2))

            n_total = len(urls)
            n_up = sum(1 for s, c in checks.items() if c.get("ok"))
            log("uptime",
                f"✓ checked {n_total} URLs · {n_up} up · {n_total - n_up} down")

            # Deploy status page
            token = _cf_token()
            if token and _ensure_project(token):
                page_html = render_status(state)
                url = _deploy_html(token, page_html)
                if url:
                    log("uptime",
                        f"✓ status page → {url} (prod: https://{PROJECT}.pages.dev/)")
        except Exception as e:
            log("uptime",
                f"⚠ cycle: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
