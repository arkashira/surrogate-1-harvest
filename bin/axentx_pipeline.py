#!/usr/bin/env python3
"""axentx pipeline — shared infra for the 5 role daemons.

Work flows through stages (each stage has its own queue dir):
    dev → review → qa → commit → done

Each daemon polls its input queue every N seconds, picks the oldest item,
processes it (calls LLM with role-specific prompt), drops the output in
the next stage's queue. No cron, no 15-min bursts — true continuous work.

Item format (JSONL one-line per file):
    {
      "id":          "20260501-081234-Costinel-discovery-a3f9",
      "project":     "Costinel",
      "focus":       "discovery|design|backend|frontend|quality|ops",
      "stage":       "dev|review|qa|commit|done",
      "created_at":  "2026-05-01T08:12:34Z",
      "history":     [{"stage":"dev","actor":"claude","output":"...","at":"..."}],
      "current":     {"text":"...latest content..."}
    }
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
SHARED = REPO_ROOT / "state" / "swarm-shared"
QUEUES = {
    "dev":     SHARED / "dev-queue",
    "review":  SHARED / "review-queue",
    "qa":      SHARED / "qa-queue",
    "commit":  SHARED / "commit-queue",
    "done":    SHARED / "done",
}
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

for q in QUEUES.values():
    q.mkdir(parents=True, exist_ok=True)


def log(role: str, msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [{role}] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"axentx-{role}-daemon.log").open("a") as f:
        f.write(line + "\n")


UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _call_surrogate_v1(prompt: str, timeout: int = 60) -> str:
    """Call user's own Surrogate-1 v1 LoRA via ashirato/surrogate-1-zero-gpu
    Gradio Space. Uses POST /call/respond → poll SSE event_id pattern."""
    space = "https://ashirato-surrogate-1-zero-gpu.hf.space"
    hf = os.environ.get("HF_TOKEN", "")
    h = {"Content-Type": "application/json", "User-Agent": UA_BROWSER}
    if hf: h["Authorization"] = f"Bearer {hf}"
    body = json.dumps({"data": [prompt[:4000]]}).encode()
    req = urllib.request.Request(f"{space}/call/respond", data=body, headers=h)
    with urllib.request.urlopen(req, timeout=10) as r:
        ev = json.loads(r.read()).get("event_id")
    if not ev: raise RuntimeError("v1: no event_id")
    poll = urllib.request.Request(f"{space}/call/respond/{ev}", headers=h)
    with urllib.request.urlopen(poll, timeout=timeout) as r:
        text = r.read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
            if payload in ("null", ""): continue
            try:
                d = json.loads(payload)
                if isinstance(d, list) and d: return str(d[0])
                if isinstance(d, str): return d
            except json.JSONDecodeError:
                return payload
    raise RuntimeError("v1: SSE returned no usable data")


def _call_gemini(prompt: str, system: str = "", max_tokens: int = 1500,
                 timeout: int = 30, model: str = "gemini-2.0-flash") -> str:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key: raise RuntimeError("no GOOGLE/GEMINI key")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": (system + "\n\n" if system else "") + prompt[:8000]}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["candidates"][0]["content"]["parts"][0]["text"]


def call_llm(prompt: str, system: str = "", max_tokens: int = 1500,
             timeout: int = 30) -> str:
    """11-provider fallback chain — burns through providers until one works.
    Order optimized: best-quality / fastest / largest free quota first."""
    # OpenAI-compatible providers (Chat Completions API shape)
    chains = [
        ("Groq", "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("SambaNova", "https://api.sambanova.ai/v1/chat/completions",
         os.environ.get("SAMBANOVA_API_KEY"), "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA-NIM", "https://integrate.api.nvidia.com/v1/chat/completions",
         os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
         "meta/llama-3.3-70b-instruct"),
        ("Kimi", "https://api.moonshot.ai/v1/chat/completions",
         os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"),
         "moonshot-v1-8k"),
        ("xAI", "https://api.x.ai/v1/chat/completions",
         os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"),
         "grok-2-1212"),
        ("Chutes", "https://llm.chutes.ai/v1/chat/completions",
         os.environ.get("CHUTES_API_KEY"), "deepseek-ai/DeepSeek-V3"),
        ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
        ("GitHub-Models", "https://models.inference.ai.azure.com/chat/completions",
         os.environ.get("GITHUB_MODELS_TOKEN"), "gpt-4o-mini"),
    ]
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:4000]})
    messages.append({"role": "user", "content": prompt[:8000]})
    payload = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    last_err = None
    for name, url, key, model in chains:
        if not key:
            continue
        body = dict(payload, model=model)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # Cerebras sits behind Cloudflare which 403s/1010 unknown UAs
                # when payload contains non-ASCII (Thai, emoji). Use a
                # browser-style UA so the WAF lets us through.
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                return d["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError,
                TimeoutError, json.JSONDecodeError) as e:
            last_err = f"{name}/{model}: {e}"
            continue

    # Gemini (different API shape — handled separately)
    try:
        return _call_gemini(prompt, system, max_tokens, timeout)
    except Exception as e:
        last_err = f"Gemini: {e} (after {last_err})"

    # Last resort: own Surrogate-1 v1 LoRA via ZeroGPU Space.
    if os.environ.get("USE_V1_FALLBACK", "1") == "1":
        try:
            full = (system + "\n\n" + prompt) if system else prompt
            return _call_surrogate_v1(full, timeout=max(timeout, 60))
        except Exception as e:
            last_err = f"surrogate-v1: {e} (after {last_err})"
    raise RuntimeError(f"all LLM providers failed; last={last_err}")


def synthesize(prompt: str, system: str = "", n_attempts: int = 3,
               max_tokens: int = 1500, timeout: int = 30) -> str:
    """Generate N candidates, then call once more to synthesize the best.
    Quality > raw call_llm at the cost of N+1 LLM credits."""
    if n_attempts < 2:
        return call_llm(prompt, system, max_tokens, timeout)
    cands = []
    for _ in range(n_attempts):
        try: cands.append(call_llm(prompt, system, max_tokens, timeout))
        except Exception: continue
    if not cands: raise RuntimeError("synthesize: no candidate succeeded")
    if len(cands) == 1: return cands[0]
    sp = ("Synthesize the best parts of multiple AI proposals. Combine the "
          "strongest insights into ONE final answer. Resolve contradictions in "
          "favor of correctness + concrete actionability.\n\n" +
          "\n\n---\n\n".join(f"Candidate {i+1}:\n{c}" for i, c in enumerate(cands)))
    return call_llm(sp, "", max_tokens, timeout)


def new_item(project: str, focus: str, prompt: str) -> dict:
    ts = datetime.datetime.utcnow()
    sid = hashlib.sha1(f"{ts.isoformat()}-{project}-{focus}".encode()).hexdigest()[:8]
    return {
        "id": f"{ts.strftime('%Y%m%d-%H%M%S')}-{project}-{focus}-{sid}",
        "project": project,
        "focus": focus,
        "stage": "dev",
        "created_at": ts.isoformat() + "Z",
        "history": [],
        "current": {"text": prompt},
    }


def write_item(item: dict, stage: str) -> Path:
    path = QUEUES[stage] / f"{item['id']}.json"
    item["stage"] = stage
    path.write_text(json.dumps(item, indent=2))
    return path


def pick_oldest(stage: str) -> tuple[Path, dict] | None:
    """Returns (path, item) for the oldest queued item, or None."""
    files = sorted(QUEUES[stage].glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            return p, json.loads(p.read_text())
        except Exception:
            # corrupt → move aside
            p.rename(p.with_suffix(".corrupt"))
            continue
    return None


def advance(item: dict, src_path: Path, next_stage: str,
            actor: str, output: str) -> Path:
    """Move item from current stage to next, append history entry."""
    item["history"].append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": output[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = output[:6000]
    src_path.unlink(missing_ok=True)
    return write_item(item, next_stage)


def fail(item: dict, src_path: Path, actor: str, err: str) -> None:
    """Mark item as failed (move to done with failure note)."""
    item["history"].append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": f"FAILED: {err}",
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    src_path.unlink(missing_ok=True)
    write_item(item, "done")


def daemon_loop(role: str, poll_sec: int, work_fn) -> None:
    """Generic daemon main — never returns. Polls input queue, runs work_fn.
    OOM-hardened: explicit gc + RSS check + bail-out before kill."""
    import gc
    import resource
    import signal
    def shutdown(*_):
        log(role, "shutdown signal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # MemoryMax in systemd is 64M; we self-restart at 48M to avoid hard kill
    SOFT_RSS_KB = int(os.environ.get("DAEMON_SOFT_RSS_KB", "49152"))  # 48 MB
    log(role, f"start — poll every {poll_sec}s, RSS soft cap {SOFT_RSS_KB} KB")
    n_processed = 0
    n_idle = 0
    while True:
        try:
            did_work = work_fn()
        except Exception as e:
            log(role, f"⚠ exception: {type(e).__name__}: {e}")
            did_work = False

        # Explicit GC after every cycle — Python releases memory only when
        # threshold hit; we want it to release immediately after LLM blob.
        gc.collect()

        # RSS check — if approaching limit, exit gracefully so systemd
        # restarts us with fresh heap (cheaper than getting OOM-killed).
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss_kb > SOFT_RSS_KB:
            log(role, f"RSS {rss_kb} KB > soft cap {SOFT_RSS_KB} KB — graceful restart")
            sys.exit(0)  # systemd Restart=always brings us back, fresh heap

        if did_work:
            n_processed += 1
            n_idle = 0
            time.sleep(2)
        else:
            n_idle += 1
            if n_idle % 20 == 1:
                log(role, f"idle (processed={n_processed} cycles, RSS={rss_kb} KB)")
            time.sleep(poll_sec)
