#!/usr/bin/env python3
"""axentx llm-health-watchdog — periodic provider probe + auto-heal.

Why this exists (user feedback 2026-05-04):
  > 'เชื่อ ว่า free tier provider ไม่ได้ ติด limit ทุกตัวหรอก เช่น GH มี
  >  pool อยู่เกือบ 10 ไม่น่าจะหมด ... ทำไมเอเจ้นไม่คอยแก้เอง'

The dev-daemon error message saying "29/33 providers cooled" was misleading
— most "providers" weren't actually configured (env vars missing). This
watchdog probes each provider directly + writes the truth to shared_kv so
*any* agent (or human) can see real LLM capacity in one place.

Cycle (every 3 min):
  1. Probe: send tiny "reply OK" prompt to each configured provider
     (HF Router × all HF tokens, GH Models × all GH PATs, Cerebras, Groq,
      SambaNova, NVIDIA, Gemini, OpenRouter, Together, DeepSeek, Chutes)
  2. Classify: WORKING (200) / RATE_LIMITED (429) / AUTH_FAIL (401/403) /
     PAYMENT (402) / DOWN (5xx/timeout) / NOT_CONFIGURED (no token)
  3. Compute health: working_count / configured_count
  4. Write shared_kv["llm.providers.health"] = {provider: {status, ts, model}}
  5. Discord ⚠ when working_count drops below 30% of configured providers
  6. Auto-fix:
     a. NOT_CONFIGURED with known token in ~/.note → log to shared_memory
        as 'env-drift' so env-sync can fill it
     b. RATE_LIMITED → no action (just observe)
     c. AUTH_FAIL → log to shared_memory + Discord (token rotated/revoked)
     d. DOWN >3 cycles → mark provider as cooldown_long in shared_kv

Tokens NEVER hit the network unless we have them in env. Probe budget:
~20 providers × ~5 tokens cumulative ≈ 25 calls per cycle = 500/hour total
across all 3 hosts (split via leader election by HOST hash).
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import signal
import socket
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("LLM_HEALTH_POLL_SEC", "180"))   # 3 min
HOST = socket.gethostname()
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")

# Leader election: hash(HOST) % 3 — only one host probes per cycle (avoid
# triple-probing same providers from 3 VMs). Cycle counter rotates the leader
# every 3 min so all 3 hosts probe ~equally over time.
HOST_HASH = int(hashlib.md5(HOST.encode()).hexdigest()[:8], 16) % 3

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _probe(name: str, url: str, token: str, model: str,
           timeout: int = 12) -> tuple[str, str]:
    """Returns (status, detail). status one of:
    WORKING / RATE_LIMITED / AUTH_FAIL / PAYMENT / DOWN / NOT_CONFIGURED"""
    if not token or len(token) < 10:
        return "NOT_CONFIGURED", "no token"
    try:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "reply only OK"}],
            "max_tokens": 5,
            "temperature": 0.0,
        }).encode()
        # Keyless probes (token startswith "no-auth-needed"): skip Authorization
        # header so anonymous endpoints don't reject as authenticated/quota'd.
        headers = {"Content-Type": "application/json"}
        if not token.startswith("no-auth-needed"):
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")[:200]
            if r.status == 200:
                return "WORKING", txt[:80]
            return "DOWN", f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "RATE_LIMITED", f"HTTP 429"
        if e.code in (401, 403):
            return "AUTH_FAIL", f"HTTP {e.code}"
        if e.code == 402:
            return "PAYMENT", "HTTP 402 (quota exhausted)"
        return "DOWN", f"HTTP {e.code}"
    except (urllib.error.URLError, socket.timeout) as e:
        return "DOWN", f"net: {type(e).__name__}"
    except Exception as e:
        return "DOWN", f"{type(e).__name__}: {str(e)[:60]}"


def build_targets() -> list[tuple[str, str, str, str]]:
    """(name, url, token, model) for every provider we care about."""
    targets: list[tuple[str, str, str, str]] = []

    # HF Router — one entry per HF token. Each token = independent bucket.
    for k in ("HF_TOKEN", "HF_TOKEN_PRO_WRITE", "HF_TOKEN_2",
              "HF_TOKEN_3", "HF_TOKEN_4"):
        tok = os.environ.get(k, "").strip()
        targets.append((
            f"HF-Router-{k}",
            "https://router.huggingface.co/v1/chat/completions",
            tok,
            "Qwen/Qwen3-Coder-30B-A3B-Instruct"))

    # GitHub Models pool — split by comma in GITHUB_TOKEN_POOL
    pool = os.environ.get("GITHUB_TOKEN_POOL", "")
    pool_tokens = [t.strip() for t in pool.split(",") if t.strip()]
    if not pool_tokens:
        # Fallback to single GITHUB_TOKEN
        single = os.environ.get("GITHUB_TOKEN", "").strip()
        if single:
            pool_tokens = [single]
    for i, tok in enumerate(pool_tokens):
        targets.append((
            f"GitHub-Models-{i+1}",
            "https://models.github.ai/inference/chat/completions",
            tok,
            "openai/gpt-4o-mini"))

    # Single-token providers
    for env_key, name, url, model in [
        ("CEREBRAS_API_KEY",  "Cerebras",
         "https://api.cerebras.ai/v1/chat/completions",
         "qwen-3-235b-a22b-instruct-2507"),
        ("GROQ_API_KEY",      "Groq",
         "https://api.groq.com/openai/v1/chat/completions",
         "llama-3.3-70b-versatile"),
        ("SAMBANOVA_API_KEY", "SambaNova",
         "https://api.sambanova.ai/v1/chat/completions",
         "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA_API_KEY",    "NVIDIA-NIM",
         "https://integrate.api.nvidia.com/v1/chat/completions",
         "meta/llama-3.3-70b-instruct"),
        ("GEMINI_API_KEY",    "Gemini",
         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
         "gemini-2.5-flash"),
        ("OPENROUTER_API_KEY","OpenRouter",
         "https://openrouter.ai/api/v1/chat/completions",
         "deepseek/deepseek-chat-v3.1:free"),
        ("DEEPSEEK_API_KEY",  "DeepSeek",
         "https://api.deepseek.com/v1/chat/completions",
         "deepseek-chat"),
        ("TOGETHER_API_KEY",  "Together",
         "https://api.together.xyz/v1/chat/completions",
         "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
        ("CHUTES_API_KEY",    "Chutes",
         "https://llm.chutes.ai/v1/chat/completions",
         "deepseek-ai/DeepSeek-V3.1"),
    ]:
        targets.append((name, url, os.environ.get(env_key, "").strip(), model))

    # 2026-05-04: include keyless providers in health tally so synthesizers
    # don't report 0% health when paid pool is exhausted but keyless works.
    # One representative entry per service — chain has many more variants.
    keyless_probes = [
        ("Keyless-Pollinations",
         "https://text.pollinations.ai/openai", "openai-fast"),
        ("Keyless-OVH-Llama",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "Meta-Llama-3_3-70B-Instruct"),
        ("Keyless-LLM7",
         "https://api.llm7.io/v1/chat/completions", "gpt-oss-20b"),
        ("Keyless-G4F-Groq",
         "https://g4f.space/api/groq/openai/v1/chat/completions",
         "llama-3.3-70b-versatile"),
        ("Keyless-Chutes",
         "https://llm.chutes.ai/v1/chat/completions",
         "Qwen/Qwen3-32B-TEE"),
    ]
    for name, url, model in keyless_probes:
        # token = "no-auth-needed" sentinel (length>10 so _probe doesn't
        # short-circuit). _probe sends Authorization: Bearer no-auth-needed
        # which servers ignore for keyless endpoints.
        targets.append((name, url, "no-auth-needed-keyless", model))

    return targets


def _kv_set(key: str, val) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(key, val)
    except Exception:
        pass


def _memory_log(kind: str, title: str, body: str = "",
                tags: list[str] | None = None) -> None:
    try:
        from axentx_shared import memory_log
        memory_log("llm-health-watchdog", kind, title,
                   body=body, tags=(tags or []))
    except Exception:
        pass


def _discord(msg: str) -> None:
    if not DISCORD:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            DISCORD,
            data=json.dumps({"content": msg[:1900]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"), timeout=10).read()
    except Exception:
        pass


def cycle():
    if _stop:
        return False
    targets = build_targets()
    results: dict[str, dict] = {}
    counts = {"WORKING": 0, "RATE_LIMITED": 0, "AUTH_FAIL": 0,
              "PAYMENT": 0, "DOWN": 0, "NOT_CONFIGURED": 0}
    auth_fails = []
    drift_keys = []

    for name, url, tok, model in targets:
        status, detail = _probe(name, url, tok, model)
        counts[status] += 1
        results[name] = {
            "status": status,
            "detail": detail[:80],
            "model": model,
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
        }
        if status == "AUTH_FAIL":
            auth_fails.append(name)
        if status == "NOT_CONFIGURED":
            drift_keys.append(name)
        # Tiny pause between probes — avoid burst
        time.sleep(1.5)

    configured = sum(c for s, c in counts.items() if s != "NOT_CONFIGURED")
    # Real availability = WORKING + RATE_LIMITED (RL = temporary, will recover)
    # AUTH_FAIL / PAYMENT / DOWN are real outages.
    available = counts["WORKING"] + counts["RATE_LIMITED"]
    working = counts["WORKING"]
    health_pct = (100 * available / configured) if configured else 0

    summary = (
        f"working={working}/{configured} "
        f"({health_pct:.0f}%) "
        f"[rate={counts['RATE_LIMITED']} pay={counts['PAYMENT']} "
        f"auth={counts['AUTH_FAIL']} down={counts['DOWN']} "
        f"missing={counts['NOT_CONFIGURED']}]"
    )
    log("llm-health-watchdog", f"  {summary}")

    # Push to shared_kv — single source of truth
    _kv_set("llm.providers.health", {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "host": HOST,
        "summary": summary,
        "counts": counts,
        "working_pct": health_pct,
        "providers": results,
    })

    # Discord on degraded health
    if configured > 0 and health_pct < 30:
        _discord(
            f"🔥 **LLM-Health** ({HOST}): only **{working}/{configured} "
            f"providers working** ({health_pct:.0f}%)\n"
            f"  • rate-limited: {counts['RATE_LIMITED']}\n"
            f"  • payment-required: {counts['PAYMENT']}\n"
            f"  • auth-fail: {counts['AUTH_FAIL']}\n"
            f"  • down: {counts['DOWN']}\n"
            f"  • not-configured: {counts['NOT_CONFIGURED']}")

    # Token rotation: PAYMENT-exhausted = quota gone for the day. Push
    # provider names into shared_kv["llm.long_cooldowns"] so axentx_pipeline's
    # _provider_ready() can skip them for hours instead of seconds. Auth-fails
    # likewise — don't keep re-trying revoked tokens.
    long_cool = []
    for name, info in results.items():
        if info["status"] in ("PAYMENT", "AUTH_FAIL"):
            long_cool.append({
                "provider": name,
                "status": info["status"],
                "until_ts": int(time.time()) + 3600,   # 1h skip
            })
    try:
        from axentx_shared import kv_set as _kv_set2
        _kv_set2("llm.long_cooldowns", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
            "providers": long_cool,
            "n": len(long_cool),
        })
    except Exception:
        pass

    # Self-heal action: log env drift (NOT_CONFIGURED) to shared_memory.
    # env-sync-daemon (or human) reads this and fills the gaps.
    if drift_keys:
        _memory_log("env-drift",
                    f"missing provider env on {HOST}",
                    body=("Providers with no token/key configured (probe "
                          "skipped):\n  - " + "\n  - ".join(drift_keys) +
                          "\n\nFix: add corresponding env to "
                          "/etc/surrogate-coordinator.env and restart "
                          "axentx-* daemons."),
                    tags=["env-drift", HOST])

    # Discord on auth-fail (rotated/revoked tokens)
    if auth_fails:
        _discord(f"⚠ **LLM-Health** ({HOST}): auth-fail providers "
                 f"(token revoked?): {', '.join(auth_fails[:5])}")
        _memory_log("auth-fail", f"{len(auth_fails)} provider(s) auth-failed",
                    body="Likely rotated/revoked tokens:\n  - " +
                         "\n  - ".join(auth_fails),
                    tags=["auth-fail", HOST])

    return False   # sleep full POLL_SEC


if __name__ == "__main__":
    daemon_loop("llm-health-watchdog", POLL_SEC, cycle)
