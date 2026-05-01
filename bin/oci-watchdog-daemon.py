#!/usr/bin/env python3
"""surrogate-watchdog — monitors fleet health, alerts to Discord."""
from __future__ import annotations
import json, os, time, urllib.request, urllib.error, signal, sys, datetime, socket

HF_TOKEN = os.environ.get("HF_TOKEN", "")
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")
COORD_HOST = os.environ.get("COORDINATOR_HOST", "")
INTERVAL = int(os.environ.get("SWEEP_INTERVAL_SEC", "300"))
COOLDOWN_SEC = int(os.environ.get("HF_COOLDOWN_SEC", "300"))
INTER_REQUEST_SLEEP = float(os.environ.get("HF_REQUEST_GAP_SEC", "1.0"))

# huggingface.co sits behind Cloudflare; default Python urllib UA gets 429-ed
# (or even 403'd) regardless of bearer token. Browser-like UA bypasses the
# anti-bot layer. Same trick we use for Supabase Management API + Cerebras.
UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SPACES = [
    "axentx/surrogate-1",
    "surrogate1/surrogate-1-shard2",
    "surrogate1/surrogate-1-zero-gpu",
    "ashirafuse1/surrogate-1-shard3",
    "ashirato/surrogate-1-zero-gpu",
    "ashirato/surrogate-1-shard1",
]

# Per-repo cooldown: skip repo until unix_ts. Hit on 429 to avoid hammering
# a rate-limited endpoint every sweep.
_repo_cooldown: dict[str, float] = {}


def get_json(url, timeout=8, repo_key: str | None = None):
    if repo_key and _repo_cooldown.get(repo_key, 0) > time.time():
        cd_left = int(_repo_cooldown[repo_key] - time.time())
        return {"_error": f"cooldown {cd_left}s remaining (last 429)"}
    headers = {"User-Agent": UA_BROWSER}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429 and repo_key:
            _repo_cooldown[repo_key] = time.time() + COOLDOWN_SEC
        return {"_error": f"HTTP {e.code}"}
    except Exception as e:
        return {"_error": str(e)}

def post_discord(msg, color=0x808080):
    if not DISCORD: return
    body = json.dumps({"embeds": [{"description": msg[:1900], "color": color, "timestamp": datetime.datetime.utcnow().isoformat()}]}).encode()
    # Discord rejects requests without a recognizable User-Agent (403 Forbidden).
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/arkashira/surrogate-1-harvest, 1.0)",
    }
    try:
        urllib.request.urlopen(urllib.request.Request(DISCORD, data=body, headers=headers), timeout=8)
    except Exception as e: print(f"[wd] discord fail: {e}", flush=True)

def shutdown(*_): print("[wd] stopping", flush=True); sys.exit(0)
signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)

print(f"[wd] start — sweep every {INTERVAL}s", flush=True)
sweep_n = 0
while True:
    sweep_n += 1
    issues = []
    # 1. HF Spaces — probe the running container subdomain directly instead
    # of calling huggingface.co/api/*. Why:
    #   - api.huggingface.co rate-limits per IP and we sit behind GCP free
    #     tier shared NAT, where thousands of other users contribute to the
    #     same IP's quota → routine 429 even with Mozilla UA + auth.
    #   - <owner>-<name>.hf.space is served by a different edge tier (the
    #     Space's own container/proxy), no global rate limit; HTTP 200 on
    #     the root path tells us the app is up. 503/timeout = down.
    # We still keep INTER_REQUEST_SLEEP + per-repo cooldown as defense in
    # depth in case the subdomain edge ever throttles too.
    n_running = 0
    n_cooldown = 0
    for i, sp in enumerate(SPACES):
        if i > 0:
            time.sleep(INTER_REQUEST_SLEEP)
        if _repo_cooldown.get(sp, 0) > time.time():
            n_cooldown += 1
            continue
        sub = sp.replace("/", "-")  # axentx/surrogate-1 → axentx-surrogate-1
        url = f"https://{sub}.hf.space/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
            # 20s timeout — Spaces waking from sleep can take 10-15s to
            # reply on first request after idle.
            with urllib.request.urlopen(req, timeout=20) as r:
                if 200 <= r.status < 400:
                    n_running += 1
                else:
                    issues.append(f"⚠ {sp} HTTP {r.status} on {url}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _repo_cooldown[sp] = time.time() + COOLDOWN_SEC
                n_cooldown += 1
            elif e.code in (502, 503, 504):
                # Space asleep / restarting — this IS a real status signal,
                # but very common (Spaces auto-sleep after 48h idle). Surface
                # it but don't escalate to ERROR color.
                issues.append(f"⚠ {sp} stage=SLEEPING (HTTP {e.code})")
            else:
                issues.append(f"HF probe fail for {sp}: HTTP {e.code}")
        except (TimeoutError, socket.timeout) as e:
            # Container hung at HTTP layer — alert ONCE then cool down so we
            # don't spam Discord every 5 min about the same broken Space.
            # Real fix is on the user's side (restart the Space from HF UI),
            # so re-alerting before cooldown expires has no value.
            if _repo_cooldown.get(sp, 0) <= time.time():
                # First time we see this — fire one alert, then start cooldown.
                issues.append(f"⚠ {sp} HTTP layer hung (timeout >20s) — needs restart")
                _repo_cooldown[sp] = time.time() + COOLDOWN_SEC
            else:
                n_cooldown += 1
        except Exception as e:
            issues.append(f"HF probe fail for {sp}: {type(e).__name__}: {str(e)[:80]}")
    # 2. Coordinator (if env set)
    if COORD_HOST:
        try:
            s = socket.create_connection((COORD_HOST, 22), timeout=4); s.close()
        except Exception as e:
            issues.append(f"⚠ coordinator {COORD_HOST}:22 unreachable: {e}")
    # 3. emit
    summary = f"[wd #{sweep_n}] {n_running}/{len(SPACES)} HF spaces running"
    if n_cooldown:
        summary += f" ({n_cooldown} in cooldown)"
    print(f"{summary} | issues={len(issues)}", flush=True)
    for it in issues: print(f"  - {it}", flush=True)
    if issues:
        post_discord(f"**watchdog #{sweep_n}**: {len(issues)} issue(s)\n" + "\n".join(f"• {i}" for i in issues), color=0xff8800)
    elif sweep_n % 12 == 1:  # heartbeat every 12 sweeps (~1h at 5min)
        post_discord(f"**watchdog #{sweep_n}** ✓ all green ({n_running}/{len(SPACES)} spaces)", color=0x00cc66)
    time.sleep(INTERVAL)
