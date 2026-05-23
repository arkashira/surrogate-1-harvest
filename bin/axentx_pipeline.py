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
import uuid
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
SHARED = REPO_ROOT / "state" / "swarm-shared"
QUEUES = {
    # Existing engineering pipeline (unchanged)
    "dev":      SHARED / "dev-queue",
    "review":   SHARED / "review-queue",
    "qa":       SHARED / "qa-queue",
    "commit":   SHARED / "commit-queue",
    "done":     SHARED / "done",
    # Product-discovery pipeline:
    # research → validator → bd → (spawn → design) | design → business →
    #   marketing → prd → dev
    # spawn = product-spawner-daemon — claims items where bd verdict is
    # NEW-PRODUCT (target_project=null), creates GitHub repo + local
    # clone, then advances to design with the real product slug. Without
    # this stage, NEW-PRODUCT items end up at commit-daemon which fails
    # with "project repo missing: /opt/axentx/null" and silently dies.
    "research":          SHARED / "research-queue",
    "validator":         SHARED / "validator-queue",
    "market-research":   SHARED / "market-research-queue",
    "bd":                SHARED / "bd-queue",
    "spawn":             SHARED / "spawn-queue",
    # 2026-05-06: lean-canvas — pre-pitch BMC + unit-economics + TAM/SAM/SOM
    # synthesis. Inserts between bd-NEW-PRODUCT and pitch so the panel
    # evaluates with concrete numbers (CAC, LTV, tiers) instead of just
    # the bd one-liner. The deeper business-synthesis still runs post-spawn.
    "lean-canvas":       SHARED / "lean-canvas-queue",
    "business-synthesis": SHARED / "business-synthesis-queue",
    # Pitch stage (added 2026-05-04): VC + Shark-Tank panel evaluates the
    # 8-doc business pack before any repo is spawned. Verdicts:
    #   GO → design (continue normal flow)
    #   PIVOT → business-synthesis (regenerate with feedback)
    #   NO-GO → done (kill before wasting dev time)
    "pitch":             SHARED / "pitch-queue",
    # Competitor-intel: per-product BuiltWith-style stack analysis +
    # competitive table. Triggered after pitch-GO, before design.
    "competitor-intel":  SHARED / "competitor-intel-queue",
    "design":            SHARED / "design-queue",
    "business":          SHARED / "business-queue",
    "marketing":         SHARED / "marketing-queue",
    "prd":               SHARED / "prd-queue",
    # 2026-05-05: added tech-lead + architect — were missing causing KeyError
    "tech-lead":         SHARED / "tech-lead-queue",
    "architect":         SHARED / "architect-queue",
    "feature-build":     SHARED / "feature-build-queue",
    "ux":                SHARED / "ux-queue",
    "design-thinking":   SHARED / "design-thinking-queue",
    # 2026-05-06 TRACK B (Non-IT Business Pipeline)
    "biz-research":      SHARED / "biz-research-queue",
    "biz-done":          SHARED / "biz-done",
    # 2026-05-10 TRACK C (Global Trend → Thai Arbitrage)
    "trend-raw":        SHARED / "trend-raw-queue",
    "trend-arbitrage": SHARED / "trend-arbitrage-queue",
    "trend-watchlist": SHARED / "trend-watchlist-queue",
    # 2026-05-11 premium biz-research queues + validation gate
    # validation-gate consumes biz-research, scores 3 dimensions:
    #   validation (revenue/funding evidence)
    #   blue-ocean (Thai market saturation)
    #   growth (TAM expansion)
    # ALL ≥7 → premium-biz-research (gold standard, deep biz plan)
    # ALL ≥5 → biz-research-validated (standard, biz-pipeline picks up)
    # Any <5 → done (KILL with rationale)
    # LLM fail → validated-watch (retry 24h)
    "validation-gate":         SHARED / "validation-gate-queue",
    "premium-biz-research":    SHARED / "premium-biz-research-queue",
    "biz-research-validated":  SHARED / "biz-research-validated-queue",
    "validated-watch":         SHARED / "validated-watch-queue",
}
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

for q in QUEUES.values():
    q.mkdir(parents=True, exist_ok=True)


def log(role: str, msg: str, **kv) -> None:
    """Dual-emit: human-readable line + structured JSON line on the SAME log
    file. Downstream tooling (jq, vector, loki) parses JSON; humans read the
    text. Optional keyword args (trace_id, item_id, ...) get embedded in the
    JSON form only — keeps the human line clean.
    """
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    text_line = f"[{ts}] [{role}] {msg}"
    json_line = json.dumps({
        "ts": ts, "role": role, "level": kv.pop("level", "info"),
        "message": msg, **kv,
    }, ensure_ascii=False)
    print(text_line, flush=True)
    with (LOG_DIR / f"axentx-{role}-daemon.log").open("a") as f:
        f.write(text_line + "\n")
        f.write(json_line + "\n")


def jlog(role: str, **kv) -> None:
    """Structured-only emitter for callers who want pure JSON (no text twin).
    Convenient in hot paths where we don't want to compose a message string.
    """
    msg = kv.pop("message", kv.pop("msg", ""))
    log(role, msg, **kv)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_role_budget(role: str, default: int) -> int:
    """Per-role token budget knob. Env BUDGET_<ROLE> overrides default.
    Roles: RESEARCH, BD, DESIGN, BUSINESS, MARKETING, PRD, DEV, REVIEWER, QA."""
    return int(os.environ.get(f"BUDGET_{role.upper()}", str(default)))


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


# 2026-05-08 fast-3 provider top-inject
# Direct fast-path providers — verified-working 2026-05-08 with curl tests.
# Used at TOP of call_llm + call_llm_strong to bypass slow chain (which
# spends 30+ seconds on ZeroGPU/HF Router timeouts before reaching working
# providers).
# 2026-05-08 multi-Gemini rotation
# Each Gemini model has an INDEPENDENT rate-limit bucket. Stack 5 models =
# ~110 RPM total free tier. Round-robin index in module global.
# 2026-05-08 GitHub Models adapter
# GitHub Models = 40+ free models via models.github.ai (PAT-authed).
# Each PAT × model = independent 150 RPD quota. With 10 PATs × 5 models
# = 7,500 daily request capacity (vs Gemini's ~110 RPM = 1.5K/day).
_GH_MODELS = [
    "meta/llama-3.3-70b-instruct",      # 70B Llama, fast, reliable
    "openai/gpt-4o-mini",                # OpenAI fast tier (free)
    "deepseek/deepseek-v3-0324",         # Strong reasoning, JSON
    "meta/llama-4-maverick-17b-128e-instruct-fp8",  # Newer Llama 4
    "mistral-ai/mistral-medium-2505",    # Different bucket
]
_GH_PATS_CACHE = None
_GH_RR_PAT = 0
_GH_RR_MODEL = 0


def _get_gh_pats():
    """Parse GITHUB_TOKEN_POOL into list of PATs (cached)."""
    global _GH_PATS_CACHE
    if _GH_PATS_CACHE is None:
        pool = os.environ.get("GITHUB_TOKEN_POOL", "")
        if not pool:
            tok = os.environ.get("GITHUB_TOKEN", "")
            _GH_PATS_CACHE = [tok] if tok else []
        else:
            _GH_PATS_CACHE = [t.strip() for t in pool.split(",") if t.strip()]
    return _GH_PATS_CACHE


def _fast_github_models(prompt: str, system: str, max_tokens: int,
                       timeout: int = 12) -> str | None:
    """Round-robin GitHub Models: 10 PATs × 5 models = 50 buckets.
    Each (PAT, model) pair has independent 150 RPD quota.

    On 429, advance to next PAT-model combo. On 200, return text + advance.
    """
    global _GH_RR_PAT, _GH_RR_MODEL
    pats = _get_gh_pats()
    if not pats:
        return None
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    # Try up to 5 different (PAT, model) combos before falling through
    for attempt in range(5):
        pat = pats[(_GH_RR_PAT + attempt) % len(pats)]
        model = _GH_MODELS[(_GH_RR_MODEL + attempt) % len(_GH_MODELS)]
        body = {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://models.github.ai/inference/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            choices = d.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content")
                if content:
                    # Advance both round-robins for next call
                    _GH_RR_PAT = (_GH_RR_PAT + 1) % len(pats)
                    _GH_RR_MODEL = (_GH_RR_MODEL + 1) % len(_GH_MODELS)
                    return content
        except urllib.error.HTTPError as e:
            # 429 → advance, try next combo. Other errors → bail.
            if e.code == 429:
                continue
            return None
        except Exception:
            return None
    # All 5 attempts exhausted (10 PATs × 5 models = 50 combos but we tried 5)
    _GH_RR_PAT = (_GH_RR_PAT + 5) % len(pats)
    _GH_RR_MODEL = (_GH_RR_MODEL + 5) % len(_GH_MODELS)
    return None


_GEMINI_MODELS = [
    "gemini-2.5-flash",          # 20 RPM
    "gemini-2.5-flash-lite",     # 30 RPM
    "gemini-2.0-flash",          # 30 RPM (legacy bucket)
    "gemini-1.5-flash",          # 15 RPM
    "gemini-1.5-flash-8b",       # 15 RPM
]
_GEMINI_RR_IDX = 0


# 2026-05-08 OpenRouter free-RR
# OpenRouter has 29 free models (verified 2026-05-08). Each model = independent
# rate-limit bucket. Round-robin = stack capacity. Free tier ~20 RPM/200 RPD
# per model, but with 5+ rotated = ~100 RPM, ~1000 RPD.
# 2026-05-08 OVHcloud anonymous
# OVHcloud AI Endpoints — totally anonymous (no signup, no API key).
# Free tier: 2 RPM per IP per model. Round-robin across 5 chat models =
# ~10 RPM aggregate per IP. EU-hosted, OpenAI SDK-compatible.
_OVH_MODELS = [
    "Meta-Llama-3_3-70B-Instruct",          # 70B Llama, decision-grade
    "Mistral-Small-3.2-24B-Instruct-2506",  # Mistral Small 3.2
    "Qwen3-32B",                             # Qwen3 32B
    "Qwen3-Coder-30B-A3B-Instruct",          # Qwen3 Coder
    "gpt-oss-120b",                          # OpenAI open-source 120B
]
_OVH_RR_IDX = 0


def _fast_ovhcloud(prompt: str, system: str, max_tokens: int,
                  timeout: int = 12) -> str | None:
    """OVHcloud AI Endpoints — no auth needed. Round-robin across 5 models.
    Returns None on failure (caller falls through to next layer)."""
    global _OVH_RR_IDX
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    # Try up to 3 models in round-robin
    for offset in range(3):
        model = _OVH_MODELS[(_OVH_RR_IDX + offset) % len(_OVH_MODELS)]
        body = {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            choices = d.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content")
                if content:
                    _OVH_RR_IDX = (_OVH_RR_IDX + 1) % len(_OVH_MODELS)
                    return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue  # try next model
            return None
        except Exception:
            return None
    _OVH_RR_IDX = (_OVH_RR_IDX + 3) % len(_OVH_MODELS)
    return None


def _fast_pollinations(prompt: str, system: str, max_tokens: int,
                      timeout: int = 12) -> str | None:
    """Pollinations text API — no auth, IP-rate-limited.
    Tries 'openai' (gpt-4o proxy) and 'mistral' models."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    for model in ("openai", "mistral", "llama"):
        body = {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://text.pollinations.ai/openai",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": UA_BROWSER},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            choices = d.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content")
                if content:
                    return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue
            return None
        except Exception:
            return None
    return None


_OR_FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",   # 120B, decision-grade
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",  # reasoning
    "google/gemma-4-31b-it:free",                # Gemma 4 31B
    "google/gemma-4-26b-a4b-it:free",            # Gemma 4 MoE
    "tencent/hy3-preview:free",                   # Tencent Hunyuan 3
    "minimax/minimax-m2.5:free",                  # MiniMax M2.5
    "inclusionai/ring-2.6-1t:free",               # Ring 2.6 (1T params!)
    "openrouter/owl-alpha",                       # OpenRouter native
    "openrouter/free",                            # Auto-pick free
    "baidu/cobuddy:free",                         # Baidu code model
]
_OR_RR_IDX = 0


def _fast_openrouter(prompt: str, system: str, max_tokens: int,
                    timeout: int = 12) -> str | None:
    """Round-robin OpenRouter free models. Returns None on failure."""
    global _OR_RR_IDX
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    # Try up to 5 different models in round-robin
    for offset in range(5):
        model = _OR_FREE_MODELS[(_OR_RR_IDX + offset) % len(_OR_FREE_MODELS)]
        body = {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
                "HTTP-Referer": "https://axentx.thinkbit.io",
                "X-Title": "axentx",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            choices = d.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content")
                if content:
                    _OR_RR_IDX = (_OR_RR_IDX + 1) % len(_OR_FREE_MODELS)
                    return content
        except urllib.error.HTTPError as e:
            if e.code in (429, 402, 503):
                continue  # quota exhausted on this model, try next
            return None
        except Exception:
            return None
    _OR_RR_IDX = (_OR_RR_IDX + 5) % len(_OR_FREE_MODELS)
    return None


def _fast_gemini_flash(prompt: str, system: str, max_tokens: int,
                      timeout: int = 12) -> str | None:
    """Gemini round-robin across 5 models = stacked rate-limit buckets.
    Each model independent quota → ~110 RPM combined free tier."""
    global _GEMINI_RR_IDX
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    full = f"{system}\n\n{prompt}" if system else prompt
    body = {
        "contents": [{"parts": [{"text": full[:24000]}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }
    # Try up to 3 models (round-robin start + 2 fallbacks). 429 → next model.
    for offset in range(3):
        model = _GEMINI_MODELS[(_GEMINI_RR_IDX + offset) % len(_GEMINI_MODELS)]
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            cands = d.get("candidates") or []
            if not cands:
                continue
            parts = (cands[0].get("content") or {}).get("parts") or []
            if not parts:
                continue
            txt = parts[0].get("text")
            if txt:
                # Advance round-robin index for next call (fairness across models)
                _GEMINI_RR_IDX = (_GEMINI_RR_IDX + 1) % len(_GEMINI_MODELS)
                return txt
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue  # try next model in round-robin
            return None
        except Exception:
            return None
    # All 3 Gemini attempts exhausted
    _GEMINI_RR_IDX = (_GEMINI_RR_IDX + 1) % len(_GEMINI_MODELS)
    return None


def _fast_groq(prompt: str, system: str, max_tokens: int,
              timeout: int = 10, model: str = "llama-3.1-8b-instant") -> str | None:
    """Groq direct (no chain). Default 8B-instant has separate TPD quota."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    body = {
        "model": model,
        "messages": [
            *([{"role": "system", "content": system[:8000]}] if system else []),
            {"role": "user", "content": prompt[:16000]},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "User-Agent": UA_BROWSER},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
    except Exception:
        return None


_NV_MODELS = [
    # # 2026-05-11 NVIDIA 8-model round-robin (verified 8 alive)
    # 8 working models (each with independent rate-limit quota).
    # Sorted by reliability/quota observed 2026-05-11 post-quota-reset:
    "meta/llama-3.1-70b-instruct",                  # 70B Llama 3.1 (~700ms)
    "meta/llama-3.1-8b-instruct",                   # 8B fast (~400ms)
    "meta/llama-3.2-3b-instruct",                   # 3B tiny (~500ms)
    "qwen/qwen2.5-coder-32b-instruct",              # 32B coder (~600ms)
    "mistralai/mixtral-8x7b-instruct-v0.1",         # MoE 8x7B (~3.5s)
    "mistralai/mixtral-8x22b-instruct-v0.1",        # MoE 8x22B (~700ms)
    "meta/llama-3.2-90b-vision-instruct",           # 90B w/ vision (~600ms)
    "abacusai/dracarys-llama-3.1-70b-instruct",     # 70B alt-finetune (~600ms)
    # NOTE: meta/llama-3.3-70b-instruct removed — chronically 429 by mid-day.
]
_NV_RR = 0


def _fast_nvidia(prompt: str, system: str, max_tokens: int,
                timeout: int = 15) -> str | None:
    """NVIDIA NIM round-robin — 8 models, each with own rate-limit pool.
    On 429, advance to next model. ~3,000 daily quota total (8 × ~400/day).
    """
    global _NV_RR
    key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not key:
        return None
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    # Try up to 4 different models per call
    for offset in range(4):
        model = _NV_MODELS[(_NV_RR + offset) % len(_NV_MODELS)]
        body = {"model": model, "messages": msgs,
                "max_tokens": max_tokens, "temperature": 0.2}
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": UA_BROWSER},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            choices = d.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content")
                if content:
                    _NV_RR = (_NV_RR + 1) % len(_NV_MODELS)
                    return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue  # next model
            return None  # other error → bail
        except Exception:
            return None
    _NV_RR = (_NV_RR + 4) % len(_NV_MODELS)
    return None



def _fast_modal_vllm(prompt: str, system: str, max_tokens: int,
                    timeout: int = 60) -> str | None:
    """Call our own Modal-hosted vLLM (Qwen2.5-Coder-7B). Highest priority
    because it's our own GPU = no rate limit per IP, cold start ~90s, warm
    ~1-2s/response. Falls through silently on any failure so cascade can
    continue.
    """
    url = os.environ.get("MODAL_VLLM_URL")
    if not url:
        return None
    if not _provider_ready("Modal-vLLM-Coder-7B"):
        return None
    api_key = os.environ.get("MODAL_VLLM_API_KEY", "axentx-modal-vllm-no-auth-2026")
    model = os.environ.get("MODAL_VLLM_MODEL", "surrogate-coder")
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:8000]})
    messages.append({"role": "user", "content": prompt[:16000]})
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.2}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": UA_BROWSER},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        c = d.get("choices") or []
        if c and c[0].get("message", {}).get("content"):
            return c[0]["message"]["content"]
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            _set_cooldown("Modal-vLLM-Coder-7B", 60)
        elif e.code in (402,):
            # Credit exhausted — long cooldown until next Modal cycle
            _set_cooldown("Modal-vLLM-Coder-7B", 6 * 3600)
    except Exception:
        _set_cooldown("Modal-vLLM-Coder-7B", 30)
    return None



def _fast_featherless(prompt: str, system: str, max_tokens: int,
                     timeout: int = 20) -> str | None:
    """Featherless.ai — 10k req/day free, OpenAI-compatible.
    Catalog has Qwen2.5-Coder, Mistral-Nemo, Llama-3.3, etc."""
    key = os.environ.get("FEATHERLESS_API_KEY")
    if not key:
        return None
    if not _provider_ready("Featherless"):
        return None
    model = os.environ.get("FEATHERLESS_MODEL",
                           "Qwen/Qwen2.5-Coder-7B-Instruct")
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:8000]})
    messages.append({"role": "user", "content": prompt[:16000]})
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.2}
    try:
        req = urllib.request.Request(
            "https://api.featherless.ai/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": UA_BROWSER},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        c = d.get("choices") or []
        if c and c[0].get("message", {}).get("content"):
            return c[0]["message"]["content"]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _set_cooldown("Featherless", 300)  # 5min for rate limit
        elif e.code in (401, 402):
            _set_cooldown("Featherless", 6 * 3600)  # 6h for auth/payment
    except Exception:
        _set_cooldown("Featherless", 60)
    return None


def _fast_cohere(prompt: str, system: str, max_tokens: int,
                timeout: int = 20) -> str | None:
    """Cohere v2 — 1k req/mo free trial. Response shape different from OpenAI:
    {"message": {"content": [{"type":"text","text":"..."}]}}
    Models: command-r-08-2024, command-a-03-2025, command-r7b-12-2024.
    """
    key = os.environ.get("COHERE_API_KEY")
    if not key:
        return None
    if not _provider_ready("Cohere"):
        return None
    model = os.environ.get("COHERE_MODEL", "command-r-08-2024")
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:8000]})
    messages.append({"role": "user", "content": prompt[:16000]})
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.2}
    try:
        req = urllib.request.Request(
            "https://api.cohere.com/v2/chat",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": UA_BROWSER},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        # Cohere v2 shape: {"message": {"content": [{"text": "..."}]}}
        msg = d.get("message") or {}
        content_arr = msg.get("content") or []
        for c in content_arr:
            if c.get("type") == "text" and c.get("text"):
                return c["text"]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _set_cooldown("Cohere", 300)
        elif e.code in (401, 402):
            _set_cooldown("Cohere", 6 * 3600)
    except Exception:
        _set_cooldown("Cohere", 60)
    return None


def _fast_3_provider_attempt(prompt: str, system: str, max_tokens: int) -> str | None:
    """Multi-provider rotation: GH Models (50 combos) + 5 Gemini + 2 Groq + NVIDIA.
    Each call rotates → fair distribution across stacked rate-limit pools.
    Median ~500ms-2s when any provider has capacity.

    Capacity stack (free tiers):
      GitHub Models  : 10 PATs × 5 models × 150 RPD = 7,500 RPD
      Gemini RR      : 5 models × 20-30 RPM = ~110 RPM = 6,600/h
      Groq 8B        : 14,400 RPD
      Groq 70B       : 1,000 RPD (often quota-exhausted)
      NVIDIA 70B     : 40 RPM (credit-based)
    """
    # Layer -1: Modal vLLM (our own GPU on Modal — Qwen2.5-Coder-7B).
    # Highest priority: zero per-request cost (within $30/mo credit), no
    # rate limit per IP. Falls through on 402 (credit exhausted) → 6h cooldown.
    r = _fast_modal_vllm(prompt, system, max_tokens, timeout=60)
    if r:
        return r
    # Layer -0.5: Featherless.ai (10k req/day free, big model catalog)
    r = _fast_featherless(prompt, system, max_tokens, timeout=20)
    if r:
        return r
    # Layer -0.3: Cohere (1k/mo free trial, command-r-08-2024)
    r = _fast_cohere(prompt, system, max_tokens, timeout=20)
    if r:
        return r
    # Layer 0: GitHub Models (highest daily capacity, decision-grade)
    r = _fast_github_models(prompt, system, max_tokens, timeout=12)
    if r:
        return r
    # Layer 0.5: OpenRouter free-model round-robin (10 free models, 5 tried/call)
    r = _fast_openrouter(prompt, system, max_tokens, timeout=12)
    if r:
        return r
    # Layer 0.7: OVHcloud anonymous (5 models RR, ~10 RPM/IP, no auth)
    r = _fast_ovhcloud(prompt, system, max_tokens, timeout=12)
    if r:
        return r
    # Layer 0.8: Pollinations (no auth, IP-rate-limited but rotates)
    r = _fast_pollinations(prompt, system, max_tokens, timeout=10)
    if r:
        return r
    # Layer 1: Gemini round-robin (5 models internal)
    r = _fast_gemini_flash(prompt, system, max_tokens, timeout=12)
    if r:
        return r
    # Layer 2: Groq 8B-instant (separate quota from 70b)
    r = _fast_groq(prompt, system, max_tokens, timeout=10,
                   model="llama-3.1-8b-instant")
    if r:
        return r
    # Layer 3: Groq 70B (often quota-exhausted by mid-day but rotates back)
    r = _fast_groq(prompt, system, max_tokens, timeout=10,
                   model="llama-3.3-70b-versatile")
    if r:
        return r
    # Layer 4: NVIDIA 70B
    r = _fast_nvidia(prompt, system, max_tokens, timeout=15)
    if r:
        return r
    return None


# Mark known-dead providers (verified 2026-05-08) so existing chain skips them
# DeepSeek 402 (insufficient balance) — would need account top-up
# Together 401 (invalid key) — would need new key from together.ai
def _mark_dead_providers_on_import():
    """Mark DeepSeek + Together with 24h cooldown so chain skips them."""
    try:
        for name in ("DeepSeek", "DeepSeek-R1", "DeepSeek-V3",
                     "Together", "Together-Llama3.3-70B-Free",
                     "Together-Qwen", "Together-Qwen2.5-72B"):
            _PROVIDER_COOLDOWN[name] = time.time() + 86400  # 24h
    except NameError:
        pass  # if _PROVIDER_COOLDOWN not yet defined, skip





SHORT_PROMPT_THRESHOLD = int(os.environ.get("SHORT_PROMPT_THRESHOLD", "500"))


def _call_cf_workers_ai(messages: list, max_tokens: int, timeout: int,
                       model: str = "@cf/meta/llama-3.1-8b-instruct") -> str:
    """Direct call to Cloudflare Workers AI (fast + cheap, ~free tier).
    Used as the preferred head of chain for short prompts."""
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not cf_token or not cf_acct:
        raise RuntimeError("CF Workers AI: missing token/account")
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{cf_acct}/ai/run/{model}",
        data=json.dumps({"messages": messages, "max_tokens": max_tokens}).encode(),
        headers={"Authorization": f"Bearer {cf_token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if not d.get("success"):
        raise RuntimeError(f"CF Workers AI: {d.get('errors')}")
    return d["result"]["response"]


# Per-provider 429 cooldown registry. When a provider returns 429 or 402,
# skip it for COOLDOWN_SEC seconds. Process-local; cleared on restart.
# (User directive 2026-05-02: 'ทำไมไม่สลับใช้ fallback อัตโนมัติ' — this
# fixes the cascade where call N+1 still hits the same dead provider.)
_PROVIDER_COOLDOWN: dict[str, float] = {}
_DEAD_PROVIDER_SET = False
_PROVIDER_FAILS: dict[str, int] = {}
# Cooldown tuning 2026-05-04 v2: with 188 concurrent dev daemons, every
# provider hits 429 at roughly the same instant → thundering herd. Bigger
# jitter (±60% instead of ±25%) spreads recovery so providers aren't all
# slammed again 30s after their cooldown clears.
_COOLDOWN_DEFAULT = 45.0
_COOLDOWN_PAYMENT = 600.0
_COOLDOWN_AUTH = 300.0
_COOLDOWN_5XX = 30.0
_COOLDOWN_MAX = 1800.0
_COOLDOWN_JITTER_FRAC = 0.60   # was 0.25
# Circuit-breaker: when ≥80% of chain is in cooldown, an extra global delay
# is added before any single LLM call to let providers recover.
_CIRCUIT_BREAKER_THRESHOLD = 0.80
_CIRCUIT_BREAKER_DELAY = 60.0


_LONG_COOL_CACHE: dict = {"ts": 0, "names": set()}


def _provider_ready(name: str) -> bool:
    # Local in-process cooldown (rate-limit recovery)
    until = _PROVIDER_COOLDOWN.get(name, 0)
    if until > time.time():
        return False
    # Cross-host long-cooldown via shared_kv (PAYMENT/AUTH_FAIL — quota
    # gone for hours). Refresh every 60s to balance freshness vs query cost.
    now_t = time.time()
    if now_t - _LONG_COOL_CACHE["ts"] > 60:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from axentx_shared import kv_get
            v = kv_get("llm.long_cooldowns") or {}
            if isinstance(v, dict) and v.get("v"): v = v["v"]
            providers = (v.get("providers") or []) if isinstance(v, dict) else []
            now_int = int(now_t)
            blocked = {p.get("provider") for p in providers
                       if isinstance(p, dict)
                       and p.get("until_ts", 0) > now_int}
            _LONG_COOL_CACHE["names"] = blocked
            _LONG_COOL_CACHE["ts"] = now_t
        except Exception:
            pass
    return name not in _LONG_COOL_CACHE["names"]


def _cooldown(name: str, sec: float | None = None) -> None:
    """Set cooldown with jitter + exponential backoff. Free no-auth
    providers get a cap of 30s (they don't have token-pool quotas — just
    transient rate-limits that recover fast). Otherwise default behavior."""
    base = sec if sec is not None else _COOLDOWN_DEFAULT
    fails = _PROVIDER_FAILS.get(name, 0) + 1
    _PROVIDER_FAILS[name] = fails
    backoff = min(base * (1.5 ** min(fails - 1, 4)), _COOLDOWN_MAX)
    # Free no-auth tier — cap cooldown at 30s. They have NO daily quota, only
    # short transient rate-limits per IP. With 80+ daemons hammering, the
    # default 1800s max kept cooling them out for entire pipeline cycles.
    no_auth_prefixes = ("Pollinations-", "OVH-", "LLM7-",
                       "OpenRouter-Free-", "ZAI-")
    if any(name.startswith(p) for p in no_auth_prefixes):
        backoff = min(backoff, 30.0)
    # Larger jitter (±60%) for thundering-herd resilience
    import random
    jitter = backoff * _COOLDOWN_JITTER_FRAC * (2 * random.random() - 1)
    _PROVIDER_COOLDOWN[name] = time.time() + backoff + jitter


def _cooldown_clear(name: str) -> None:
    _PROVIDER_COOLDOWN.pop(name, None)
    _PROVIDER_FAILS.pop(name, None)


def _circuit_breaker_check(total_providers: int = 33) -> bool:
    """Return True if circuit is OPEN (most providers cooled). Caller
    should sleep extra to let providers breathe."""
    now = time.time()
    cooled = sum(1 for v in _PROVIDER_COOLDOWN.values() if v > now)
    if total_providers > 0 and cooled / total_providers >= _CIRCUIT_BREAKER_THRESHOLD:
        # Add a small per-process delay; with 188 daemons, 60s × jitter
        # spreads the herd
        import random
        delay = _CIRCUIT_BREAKER_DELAY + random.random() * _CIRCUIT_BREAKER_DELAY
        time.sleep(delay)
        return True
    return False


def _codespace_urls() -> list[str]:
    """Returns the list of codespace ollama endpoints in priority order.

    Multi-codespace fleet (2026-05-02 round 2): each free GH account gets
    60h/mo of codespace runtime. With 7 codespace-eligible accounts in
    rotation we have ~420h/mo of LLM proxy capacity. Specify all of them
    via CODESPACE_LLM_URLS (comma-separated) — pipeline picks the first
    one whose cooldown is clear. Falls back to CODESPACE_LLM_URL (single)
    for backwards compat.
    """
    multi = os.environ.get("CODESPACE_LLM_URLS", "").strip()
    if multi:
        return [u.strip() for u in multi.split(",") if u.strip()]
    single = os.environ.get("CODESPACE_LLM_URL", "").strip()
    return [single] if single else []


def _call_codespace_ollama(messages: list, max_tokens: int, timeout: int) -> str:
    """Try each codespace endpoint in turn; first one whose per-URL cooldown
    is clear gets the request. On failure, mark just that URL as cooling
    and try the next — don't fail the whole call until ALL endpoints are
    cooling.

    URL list via env CODESPACE_LLM_URLS (comma-separated) or single
    CODESPACE_LLM_URL. Each codespace runs ollama with the same model
    (qwen2.5-coder:7b-instruct-q4_K_M) so requests are interchangeable.

    Codespaces auto-stop after 30min idle to preserve the free quota.
    First call after wake takes ~30-60s; subsequent calls are 2-5s.
    """
    urls = _codespace_urls()
    if not urls:
        raise RuntimeError("no CODESPACE_LLM_URLS")
    model = os.environ.get("CODESPACE_LLM_MODEL", "qwen2.5-coder:7b-instruct-q4_K_M")
    last_err = "no endpoint tried"
    for i, base in enumerate(urls):
        provider_key = f"Codespace-LLM-{i}"
        if not _provider_ready(provider_key):
            continue
        url = base.rstrip("/") + "/v1/chat/completions"
        body = {"model": model, "messages": messages,
                "max_tokens": max_tokens, "temperature": 0.3}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            # 502 = codespace asleep; 429/503 = ollama overloaded. Different
            # cooldowns: 502 short (codespace wakes in ~30s), 429 longer.
            sec = 30 if e.code == 502 else _COOLDOWN_DEFAULT
            _cooldown(provider_key, sec)
            last_err = f"{provider_key}: HTTP {e.code}"
        except Exception as e:
            _cooldown(provider_key, 60)
            last_err = f"{provider_key}: {type(e).__name__}: {str(e)[:60]}"
    raise RuntimeError(f"all codespace endpoints down: {last_err}")


def _hf_inference(messages: list, max_tokens: int, timeout: int,
                  model: str | None = None) -> str:
    """Hugging Face Serverless Inference Router — uses 3rd-party providers
    (Novita / Together / Fireworks / DeepInfra) with HF_TOKEN auth.

    Default model: 'inclusionAI/Ling-2.6-1T' on Novita (pricing 0/0 = FREE).
    Independent budget from CF/Groq/etc.
    """
    tok = os.environ.get("HF_TOKEN", "")
    if not tok:
        raise RuntimeError("no HF_TOKEN")
    model = model or os.environ.get("HF_ROUTER_MODEL", "inclusionAI/Ling-2.6-1T")
    url = "https://router.huggingface.co/v1/chat/completions"
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.3}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json",
                 "User-Agent": UA_BROWSER},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def _semcache_get_or_set(prompt: str, system: str, max_tokens: int):
    """Returns (cached_response_or_None, cache_key_for_set)."""
    try:
        from axentx_semantic_cache import cache_get
        # Round max_tokens to bucket so similar requests share cache
        bucket = (max_tokens // 256) * 256
        key = (system or "", prompt, bucket)
        return cache_get(key), key
    except Exception:
        return None, None


def call_llm(prompt: str, system: str = "", max_tokens: int = 1500,
             timeout: int = 30) -> str:
    """Multi-provider fallback chain with per-provider cooldown.

    Layered routing (added 2026-05-04 from research):
      1. Semantic cache hit → return immediately (0 LLM calls)
      2. LiteLLM proxy if LITELLM_PROXY_URL env set (unified gateway)
      3. Existing chain fallback (70+ endpoints)
    """
    # # 2026-05-08 fast-3 provider top-inject — mark dead providers once per process
    global _DEAD_PROVIDER_SET
    if not _DEAD_PROVIDER_SET:
        _mark_dead_providers_on_import()
        _DEAD_PROVIDER_SET = True
    # Layer 1 — semantic cache (exact hash)
    cached, sem_key = _semcache_get_or_set(prompt, system, max_tokens)
    if cached:
        return cached
    # # 2026-05-11 near-dup cache wire (jaccard ≥0.95 layer)
    # Layer 1.1 — near-duplicate match (jaccard on 3-grams ≥ 0.95).
    # Catches reformatted prompts (whitespace, case, punctuation shuffles).
    try:
        from axentx_semantic_cache import near_get
        near_hit = near_get(system or "", prompt, max_tokens)
        if near_hit:
            return near_hit
    except Exception:
        pass
    # Layer 1.5 — fast 3-provider direct (Gemini Flash, Groq 8B, NVIDIA).
    # Skips slow chain (ZeroGPU + HF Router etc) which can timeout 60s
    # before reaching working providers. # 2026-05-08 fast-3 provider top-inject
    fast = _fast_3_provider_attempt(prompt, system, max_tokens)
    if fast:
        if sem_key:
            try:
                from axentx_semantic_cache import cache_set, near_set
                cache_set(sem_key, fast, ttl_sec=3600)
                near_set(system or "", prompt, max_tokens, fast, ttl_sec=3600)
            except Exception:
                pass
        return fast
    # Layer 2 — LiteLLM proxy (if operator self-hosted it)
    try:
        from axentx_litellm import call as litellm_call
        if os.environ.get("LITELLM_PROXY_URL"):
            messages = []
            if system: messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            r = litellm_call(messages, max_tokens=max_tokens, timeout=timeout)
            if r:
                if sem_key:
                    try:
                        from axentx_semantic_cache import cache_set, near_set
                        cache_set(sem_key, r, ttl_sec=3600)
                        near_set(system or "", prompt, max_tokens, r, ttl_sec=3600)
                    except Exception: pass
                return r
    except Exception:
        pass
    # Layer 3 — existing chain (70+ endpoints fallthrough below)
    _orig_docstring = """Multi-provider fallback chain with per-provider cooldown.

    Order: fastest free → 70B class free → CF Workers AI → Gemini → HF
    Inference → Surrogate-1 v1 (LAST resort, ALWAYS tried even when v1
    quality is lower than alternatives, per user directive 2026-05-02:
    'ให้ตายยังไงมันก็มี model ทำงานได้').

    Per-provider cooldown: any 429/402/5xx puts the provider in cooldown
    for 2 min (rate limit) or 24 h (payment). Other calls during cooldown
    skip the dead provider — no more 'try Groq, fail, try Cerebras, fail,
    try Groq again' cascades.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system[:4000]})
    messages.append({"role": "user", "content": prompt[:8000]})

    # Fast path: short prompts get Workers AI first.
    if len(prompt) < SHORT_PROMPT_THRESHOLD and _provider_ready("CF-AI-fastpath"):
        try:
            return _call_cf_workers_ai(messages, max_tokens, timeout)
        except Exception:
            _cooldown("CF-AI-fastpath", 60)

    # OpenAI-compatible providers.
    # CF AI Gateway listed FIRST — cached responses (5min TTL by default)
    # for repeat queries massively reduce upstream calls. AI Gateway then
    # round-robins to its configured upstreams (Workers AI, OpenAI, etc).
    # Cooldown if the gateway itself fails — fall through to direct
    # provider list. Token: CLOUDFLARE_API_TOKEN (or new hermess token).
    cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = (os.environ.get("CLOUDFLARE_AI_GATEWAY_TOKEN")
                or os.environ.get("CLOUDFLARE_API_TOKEN", ""))
    cf_gw = os.environ.get("CF_AI_GATEWAY_NAME", "axentx-llm")

    # CF AI Gateway proxy URLs — same providers, but routed through gateway
    # for caching (5min TTL = repeat-query free), rate-limit smoothing, and
    # observability. Verified 2026-05-03: gateway 'axentx-llm' created with
    # token edit-scope. Gateway proxies to upstream providers using their
    # original API key (cf_token ignored on inference, used only on the
    # gateway hop for auth+caching).
    def _cf_proxy(provider_path: str) -> str:
        return (f"https://gateway.ai.cloudflare.com/v1/{cf_acct}/{cf_gw}/"
                f"{provider_path}") if cf_acct else ""

    # ── PRIORITY 1: dedicated ZeroGPU Spaces (no rate limits, PRO entitlement) ─
    # 2 Spaces = 50K H200 minutes/month combined. Each call ~3-8s, no API
    # rate limit, no per-day quota — only ZeroGPU minutes which are HUGE.
    space_1 = "https://surrogate1-coder-zero-gpu-1.hf.space/v1/chat/completions"
    space_2 = "https://ashirato-coder-zero-gpu-2.hf.space/v1/chat/completions"

    # ── HF Router with EVERY token user has — each = independent bucket ──
    # Built dynamically so adding HF_TOKEN_5/6/7 to env auto-extends chain.
    hf_tokens = []
    for k in ("HF_TOKEN_PRO", "HF_TOKEN_PRO_WRITE", "HF_TOKEN", "HF_TOKEN_2",
              "HF_TOKEN_3", "HF_TOKEN_4", "HF_TOKEN_LEGACY"):
        v = os.environ.get(k, "").strip()
        if v and len(v) > 20 and v not in [t for _, t in hf_tokens]:
            hf_tokens.append((k, v))
    hf_pro_1 = hf_tokens[0][1] if hf_tokens else ""
    hf_pro_2 = hf_tokens[1][1] if len(hf_tokens) > 1 else hf_pro_1

    # ZeroGPU Spaces — opt-in via env (set to "1" only when Space stage=RUNNING).
    # Both Spaces have repeatedly hit RUNTIME_ERROR (gradio app crashes on startup
    # under zero-a10g hw); 503s from broken Spaces just burn cooldown windows
    # without producing a working response. Default OFF; portfolio-syncer +
    # auto-healer will probe Space stage and flip this on when healthy.
    spaces_enabled = os.environ.get("ZEROGPU_SPACES_ENABLED", "0") == "1"
    spaces_chain = ([
        ("ZeroGPU-Coder-1", space_1, hf_pro_1, "axentx-coder-1"),
        ("ZeroGPU-Coder-2", space_2, hf_pro_2, "axentx-coder-2"),
    ] if spaces_enabled else [])

    chains = spaces_chain + [
        # ── HF Router Qwen3-Coder per token (parallel rate-limit buckets) ──
        (f"HF-Router-Qwen3-Coder-{i+1}",
         "https://router.huggingface.co/v1/chat/completions",
         tok, "Qwen/Qwen3-Coder-30B-A3B-Instruct")
        for i, (_, tok) in enumerate(hf_tokens)
    ] + [
        # Bigger general models on the same Router (different model = different bucket)
        ("HF-Router-Qwen3-235B",
         "https://router.huggingface.co/v1/chat/completions",
         hf_pro_1, "Qwen/Qwen3.6-35B-A3B"),
        ("HF-Router-DeepSeek-V4",
         "https://router.huggingface.co/v1/chat/completions",
         hf_pro_2, "deepseek-ai/DeepSeek-V4-Flash"),
        ("HF-Router-Kimi-K2",
         "https://router.huggingface.co/v1/chat/completions",
         hf_pro_1, "moonshotai/Kimi-K2.6"),
        ("HF-Router-Ling-1T",
         "https://router.huggingface.co/v1/chat/completions",
         hf_pro_2, "inclusionAI/Ling-2.6-1T"),
        # Through CF Gateway — caches identical prompts. Workers AI quota
        # already exhausted today (10K neurons), so listed but cooldown'd.
        ("CF-Gateway-WAI", _cf_proxy("workers-ai/v1/chat/completions"),
         cf_token, "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
        # Same Cerebras call but through gateway — caching + observability.
        # Same upstream key, gateway just records + caches.
        ("CF-Gateway-Cerebras", _cf_proxy("cerebras/v1/chat/completions"),
         os.environ.get("CEREBRAS_API_KEY"),
         "qwen-3-235b-a22b-instruct-2507"),
        ("CF-Gateway-Groq", _cf_proxy("groq/openai/v1/chat/completions"),
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Groq",          "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        # Cerebras 2026-05-03: llama-3.3-70b removed, qwen-3-235b-a22b
        # is the new biggest+fastest model on free tier. gpt-oss-120b
        # added as separate entry for parallel rate-limit bucket.
        ("Cerebras",      "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"),
         "qwen-3-235b-a22b-instruct-2507"),
        ("Cerebras-GPT",  "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "gpt-oss-120b"),
        ("Cerebras-Llama", "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama3.1-8b"),
        ("SambaNova",     "https://api.sambanova.ai/v1/chat/completions",
         os.environ.get("SAMBANOVA_API_KEY"), "Meta-Llama-3.3-70B-Instruct"),
        ("NVIDIA-NIM",    "https://integrate.api.nvidia.com/v1/chat/completions",
         os.environ.get("NVIDIA_NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
         "meta/llama-3.3-70b-instruct"),
        # Kimi removed 2026-05-02: KIMI_API_KEY auth fails ('Invalid
        # Authentication'). Re-add when user rotates the key.
        ("OpenRouter",    "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "meta-llama/llama-3.3-70b-instruct:free"),
        # DeepSeek native (added 2026-05-03). Free signup credits, then
        # off-peak (UTC 16:30-00:30) discount. Cost-guard catches any
        # overage. deepseek-chat = V3, deepseek-reasoner = R1 (slower).
        ("DeepSeek",      "https://api.deepseek.com/v1/chat/completions",
         os.environ.get("DEEPSEEK_API_KEY"), "deepseek-chat"),
        ("DeepSeek-R1",   "https://api.deepseek.com/v1/chat/completions",
         os.environ.get("DEEPSEEK_API_KEY"), "deepseek-reasoner"),
        # Together.ai trial (added 2026-05-03). $1 free trial on signup,
        # rotate accounts (ashirapit / ashira-devops / ashira-fuse pattern)
        # for continuous free coverage. Lite model = cheapest per-token.
        ("Together",      "https://api.together.xyz/v1/chat/completions",
         os.environ.get("TOGETHER_API_KEY"),
         "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
        ("Together-Qwen", "https://api.together.xyz/v1/chat/completions",
         os.environ.get("TOGETHER_API_KEY"),
         "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        # GitHub Models — 150 calls/day per token. Pool rotation: try every
        # token in GITHUB_TOKEN_POOL (4 tokens × 150 = 600 calls/day cap).
        # Each entry uses a different token = independent rate-limit bucket.
    ] + [
        (f"GitHub-Models-{i+1}",
         "https://models.inference.ai.azure.com/chat/completions",
         tok.strip(), "gpt-4o-mini")
        for i, tok in enumerate(
            (os.environ.get("GITHUB_TOKEN_POOL", "")
             or os.environ.get("GITHUB_MODELS_TOKEN", "")
             or os.environ.get("GITHUB_TOKEN", "")
             ).split(","))
        if tok.strip() and len(tok.strip()) > 20
    ] + [
        # Mistral free tier — separate budget pool from everything above.
        ("Mistral",       "https://api.mistral.ai/v1/chat/completions",
         os.environ.get("MISTRAL_API_KEY"), "mistral-small-latest"),
        # ★ ALWAYS-FREE BACKSTOPS (added 2026-05-04 — user asked: 'มีที่ไหน
        # มี model ฟรีให้ใช้ตลอดมะ ติด limit หมดแล้วทุก pool'). These have
        # NO auth (Pollinations) or use OpenRouter ':free' tier with no
        # daily quota tied to our token pool — bypasses the per-token
        # exhaustion that hits HF/Cerebras/Groq/etc. Use a stub key so the
        # chain doesn't filter them out for `c[2] and ...`.
        ("Pollinations-GPT-OSS-20B",
         "https://text.pollinations.ai/openai",
         "no-auth-needed",   # actually no auth required
         "openai-fast"),
        # More Pollinations model variants — same endpoint, different model
        # name = different upstream LLM. All confirmed to EXIST 2026-05-04.
        ("Pollinations-Evil",
         "https://text.pollinations.ai/openai",
         "no-auth-needed", "evil"),
        ("Pollinations-Unity",
         "https://text.pollinations.ai/openai",
         "no-auth-needed", "unity"),
        ("Pollinations-Bidara",
         "https://text.pollinations.ai/openai",
         "no-auth-needed", "bidara"),
        ("Pollinations-Rtist",
         "https://text.pollinations.ai/openai",
         "no-auth-needed", "rtist"),
        ("Pollinations-Elixposearch",
         "https://text.pollinations.ai/openai",
         "no-auth-needed", "elixposearch"),
        # ★ OVHcloud AI Endpoints — anonymous, EU-hosted, 40+ models
        # 2 RPM per IP per model — distribute across many models so we
        # never hit per-model limit. Confirmed 2026-05-04.
        ("OVH-GPT-OSS-120B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "gpt-oss-120b"),
        ("OVH-GPT-OSS-20B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "gpt-oss-20b"),
        ("OVH-Qwen3-Coder-30B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Qwen3-Coder-30B-A3B-Instruct"),
        ("OVH-Llama-3.3-70B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Meta-Llama-3_3-70B-Instruct"),
        ("OVH-Qwen3-32B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Qwen3-32B"),
        ("OVH-Mistral-Small-24B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Mistral-Small-3.2-24B-Instruct-2506"),
        ("OVH-Llama-3.1-8B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Llama-3.1-8B-Instruct"),
        ("OVH-Mistral-7B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Mistral-7B-Instruct-v0.3"),
        # ★ LLM7.io — anonymous aggregator, 30 RPM per IP, 30+ models
        ("LLM7-GPT-OSS-20B",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "gpt-oss-20b"),
        ("LLM7-GPT-4o-Mini",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "gpt-4o-mini"),
        ("LLM7-DeepSeek",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "deepseek-v3"),
        ("LLM7-Mistral",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "mistral-large"),
        ("LLM7-Gemini",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "gemini-2.5-flash"),
        ("LLM7-Qwen",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "qwen-coder"),
        # ★ z.ai (Zhipu international) — confirmed 2026-05-04
        # 1M tokens/day per model on free tier with signup. International
        # endpoint avoids CN region IP blocks.
        ("ZAI-GLM-4.5-Flash",
         "https://api.z.ai/api/paas/v4/chat/completions",
         os.environ.get("ZAI_API_KEY"), "glm-4.5-flash"),
        ("ZAI-GLM-4.6V-Flash",
         "https://api.z.ai/api/paas/v4/chat/completions",
         os.environ.get("ZAI_API_KEY"), "glm-4.6v-flash"),
        ("ZAI-GLM-4.7-Flash",
         "https://api.z.ai/api/paas/v4/chat/completions",
         os.environ.get("ZAI_API_KEY"), "glm-4.7-flash"),
        ("ZAI-GLM-4-Plus",
         "https://api.z.ai/api/paas/v4/chat/completions",
         os.environ.get("ZAI_API_KEY"), "glm-4-plus"),
        ("OpenRouter-Free-GPT-OSS-120B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "openai/gpt-oss-120b:free"),
        ("OpenRouter-Free-MiniMax-M2.5",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "minimax/minimax-m2.5:free"),
        ("OpenRouter-Free-Qwen3-Next-80B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "qwen/qwen3-next-80b-a3b-instruct:free"),
        ("OpenRouter-Free-NVIDIA-Nemotron-120B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "nvidia/nemotron-3-super-120b-a12b:free"),
        ("OpenRouter-Free-GLM-4.5-Air",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "z-ai/glm-4.5-air:free"),
        # Additional OpenRouter free tested 2026-05-04 — all returned 200
        ("OpenRouter-Free-Gemma-3N-E4B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "google/gemma-3n-e4b-it:free"),
        ("OpenRouter-Free-GPT-OSS-20B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "openai/gpt-oss-20b:free"),
        ("OpenRouter-Free-Ling-2.6-1T",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "inclusionai/ling-2.6-1t:free"),
        ("OpenRouter-Free-Liquid-LFM-2.5",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "liquid/lfm-2.5-1.2b-instruct:free"),
        ("OpenRouter-Free-Nemotron-Nano-9B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "nvidia/nemotron-nano-9b-v2:free"),
        ("OpenRouter-Free-Nemotron-Nano-30B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "nvidia/nemotron-3-nano-30b-a3b:free"),
        ("OpenRouter-Free-Gemma-3-12B",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "google/gemma-3-12b-it:free"),
        ("OpenRouter-Free-Qwen3-Coder",
         "https://openrouter.ai/api/v1/chat/completions",
         os.environ.get("OPENROUTER_API_KEY"),
         "qwen/qwen3-coder:free"),
        # 2026-05-04 PHASE 2 keyless expansion — user: "haa free maa peum mai por"
        # Pollinations: each model name = independent rate-limit bucket on
        # their backend. All probed return 429 (recognized) 2026-05-04.
        ("Pollinations-Haiku",
         "https://text.pollinations.ai/openai", "no-auth-needed", "haiku"),
        ("Pollinations-Grok",
         "https://text.pollinations.ai/openai", "no-auth-needed", "grok"),
        ("Pollinations-Grok-3",
         "https://text.pollinations.ai/openai", "no-auth-needed", "grok-3"),
        ("Pollinations-DeepSeek",
         "https://text.pollinations.ai/openai", "no-auth-needed", "deepseek"),
        ("Pollinations-DeepSeek-V3",
         "https://text.pollinations.ai/openai", "no-auth-needed", "deepseek-v3"),
        ("Pollinations-DeepSeek-Coder",
         "https://text.pollinations.ai/openai", "no-auth-needed", "deepseek-coder"),
        ("Pollinations-O1",
         "https://text.pollinations.ai/openai", "no-auth-needed", "o1"),
        ("Pollinations-O3",
         "https://text.pollinations.ai/openai", "no-auth-needed", "o3"),
        ("Pollinations-ChatGPT-4o",
         "https://text.pollinations.ai/openai", "no-auth-needed", "chatgpt-4o"),
        ("Pollinations-GPT-5",
         "https://text.pollinations.ai/openai", "no-auth-needed", "gpt-5"),
        ("Pollinations-Sao",
         "https://text.pollinations.ai/openai", "no-auth-needed", "sao"),
        ("Pollinations-SearchGPT",
         "https://text.pollinations.ai/openai", "no-auth-needed", "searchgpt"),
        ("Pollinations-Llamascout",
         "https://text.pollinations.ai/openai", "no-auth-needed", "llamascout"),
        ("Pollinations-Llama-3.3",
         "https://text.pollinations.ai/openai", "no-auth-needed", "llama-3.3"),
        ("Pollinations-Qwen3",
         "https://text.pollinations.ai/openai", "no-auth-needed", "qwen3"),
        ("Pollinations-Qwen-2.5",
         "https://text.pollinations.ai/openai", "no-auth-needed", "qwen-2.5"),
        ("Pollinations-Yi",
         "https://text.pollinations.ai/openai", "no-auth-needed", "yi"),
        ("Pollinations-CodeQwen",
         "https://text.pollinations.ai/openai", "no-auth-needed", "codeqwen"),
        ("Pollinations-Sur",
         "https://text.pollinations.ai/openai", "no-auth-needed", "sur"),
        ("Pollinations-Sur-Mistral",
         "https://text.pollinations.ai/openai", "no-auth-needed", "sur-mistral"),
        # LLM7 — 2 new models from /v1/models (confirmed answering 2026-05-04)
        ("LLM7-Codestral",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "codestral-latest"),
        ("LLM7-GLM-4.6V-Flash",
         "https://api.llm7.io/v1/chat/completions",
         "no-auth-needed", "GLM-4.6V-Flash"),
        # OVH — 4 more models from /v1/models (separate RL buckets per model)
        ("OVH-Qwen3.5-9B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Qwen3.5-9B"),
        ("OVH-Mistral-Nemo",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Mistral-Nemo-Instruct-2407"),
        ("OVH-Qwen2.5-VL-72B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Qwen2.5-VL-72B-Instruct"),
        ("OVH-Qwen3Guard-0.6B",
         "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "no-auth-needed", "Qwen3Guard-Gen-0.6B"),
        # 2026-05-04 PHASE 3 keyless expansion (deep search wave 2):
        # g4f.space proxy provides keyless access to upstreams that normally
        # require keys. Probed working: Groq, Gemini, Perplexity, Ollama-pool.
        # Note: 5 RPM/IP global on g4f.space — these are last-resort entries
        # below the primary chain. When paid pool 402/429s, these still answer.
        ("G4F-Groq-Llama-3.3-70B",
         "https://g4f.space/api/groq/openai/v1/chat/completions",
         "no-auth-needed", "llama-3.3-70b-versatile"),
        ("G4F-Gemini-2.5-Flash",
         "https://g4f.space/api/gemini/v1/chat/completions",
         "no-auth-needed", "gemini-2.5-flash"),
        ("G4F-Gemini-2.5-Pro",
         "https://g4f.space/api/gemini/v1/chat/completions",
         "no-auth-needed", "gemini-2.5-pro"),
        ("G4F-Perplexity-Turbo",
         "https://g4f.space/api/perplexity/v1/chat/completions",
         "no-auth-needed", "turbo"),
        ("G4F-Ollama-Gemma3-4B",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "gemma3:4b"),
        ("G4F-Ollama-Gemma3-12B",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "gemma3:12b"),
        ("G4F-Ollama-Kimi-K2.6",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "kimi-k2.6"),
        ("G4F-Ollama-DeepSeek-V4-Pro",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "deepseek-v4-pro"),
        ("G4F-Ollama-MiniMax-M2.5",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "minimax-m2.5"),
        ("G4F-Ollama-GPT-OSS-120B",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "gpt-oss:120b"),
        ("G4F-Ollama-Qwen3-Next-80B",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "qwen3-next:80b"),
        ("G4F-Ollama-Nemotron-3-Super",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "nemotron-3-super"),
        ("G4F-Ollama-GLM-5.1",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "glm-5.1"),
        ("G4F-Ollama-Devstral-2-123B",
         "https://g4f.space/api/ollama/v1/chat/completions",
         "no-auth-needed", "devstral-2:123b"),
        # Chutes.ai keyless: /v1/models open + /v1/chat/completions reachable
        # (429 = rate-limited per IP, works after cooldown). TEE-hosted big
        # models — adds DeepSeek-V3.1, Qwen3.5-397B, Kimi-K2.5 backstop.
        ("Chutes-Qwen3-32B",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "Qwen/Qwen3-32B-TEE"),
        ("Chutes-Qwen3.5-397B",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "Qwen/Qwen3.5-397B-A17B-TEE"),
        ("Chutes-DeepSeek-V3.1",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "deepseek-ai/DeepSeek-V3.1-TEE"),
        ("Chutes-Kimi-K2.5",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "moonshotai/Kimi-K2.5-TEE"),
        ("Chutes-GLM-5.1",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "zai-org/GLM-5.1-TEE"),
        ("Chutes-MiniMax-M2.5",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "MiniMaxAI/MiniMax-M2.5-TEE"),
        ("Chutes-Gemma-4-31B",
         "https://llm.chutes.ai/v1/chat/completions",
         "no-auth-needed", "google/gemma-4-31B-turbo-TEE"),
    ]
    # Skip providers in cooldown — round-robin to first available.
    chains_ready = [c for c in chains if c[2] and _provider_ready(c[0])]
    # Circuit breaker: if most providers cooled, sleep before hammering
    # the few that are ready (188 dev daemons would otherwise re-trip).
    _circuit_breaker_check(total_providers=len(chains))
    last_err = None
    payload = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    for name, url, key, model in chains_ready:
        body = dict(payload, model=model)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": UA_BROWSER,
        }
        # Pollinations needs no auth; skip Authorization header for it.
        if not (name.startswith("Pollinations") or key == "no-auth-needed"):
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                _cooldown_clear(name)
                return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 402:
                _cooldown(name, _COOLDOWN_PAYMENT)
            elif e.code in (401, 403):
                _cooldown(name, _COOLDOWN_AUTH)
            elif e.code == 429:
                # Honor Retry-After if present
                ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                _cooldown(name, int(ra) if (ra and ra.isdigit()) else _COOLDOWN_DEFAULT)
            elif 500 <= e.code < 600:
                _cooldown(name, 60)
            last_err = f"{name}/{model}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, KeyError, TimeoutError,
                json.JSONDecodeError) as e:
            _cooldown(name, 60)
            last_err = f"{name}/{model}: {type(e).__name__}: {str(e)[:60]}"
            continue

    # Cloudflare Workers AI (8B) — 9th provider, 10k neurons/day free
    if _provider_ready("CF-AI"):
        cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
        cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if cf_token and cf_acct:
            cf_model = os.environ.get("CF_AI_MODEL", "@cf/meta/llama-3.1-8b-instruct")
            try:
                req = urllib.request.Request(
                    f"https://api.cloudflare.com/client/v4/accounts/{cf_acct}/ai/run/{cf_model}",
                    data=json.dumps({"messages": messages, "max_tokens": max_tokens}).encode(),
                    headers={"Authorization": f"Bearer {cf_token}",
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                    if d.get("success"):
                        return d["result"]["response"]
                    last_err = f"CF-AI/{cf_model}: {d.get('errors')}"
                    _cooldown("CF-AI", 60)
            except urllib.error.HTTPError as e:
                if e.code in (429, 402):
                    _cooldown("CF-AI", _COOLDOWN_DEFAULT)
                last_err = f"CF-AI/{cf_model}: HTTP {e.code} (after {last_err})"
            except Exception as e:
                _cooldown("CF-AI", 60)
                last_err = f"CF-AI/{cf_model}: {e} (after {last_err})"

    # HF Serverless Inference API (Ling-2.6-1T) — 10th provider, free tier
    # 1T-param model on Novita via HF Router, separate budget.
    if _provider_ready("HF-Inference"):
        try:
            return _hf_inference(messages, max_tokens, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                _cooldown("HF-Inference", _COOLDOWN_DEFAULT)
            last_err = f"HF-Inference: HTTP {e.code} (after {last_err})"
        except Exception as e:
            _cooldown("HF-Inference", 60)
            last_err = f"HF-Inference: {e} (after {last_err})"

    # Codespace fleet — multiple ollama endpoints across free GH accounts.
    # Each endpoint has its own per-URL cooldown so a single sleeping
    # codespace doesn't poison the whole fleet. Round-robin internal to
    # _call_codespace_ollama — only fails when ALL urls are cooling.
    if _codespace_urls():
        try:
            return _call_codespace_ollama(messages, max_tokens, max(timeout, 60))
        except RuntimeError as e:
            last_err = f"Codespace-fleet: {e} (after {last_err})"
        except Exception as e:
            last_err = f"Codespace-fleet: {type(e).__name__}: {str(e)[:80]} (after {last_err})"

    # Gemini (different API shape — handled separately)
    if _provider_ready("Gemini"):
        try:
            return _call_gemini(prompt, system, max_tokens, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 402):
                _cooldown("Gemini", _COOLDOWN_DEFAULT)
            last_err = f"Gemini: HTTP {e.code} (after {last_err})"
        except Exception as e:
            _cooldown("Gemini", 60)
            last_err = f"Gemini: {e} (after {last_err})"

    # Surrogate-1 v1 fallback (HF ZeroGPU Space).
    # Default OFF as of 2026-05-02 because the Space's GPU function
    # currently returns 'event: error data: null' (ZeroGPU quota exhausted
    # or LoRA load failure on the Space side — not our chain bug). The
    # Ling-2.6-1T router call above already serves the 'always-something
    # answers' role with a 1T-param model that's better than v1's 7B.
    # Re-enable via USE_V1_FALLBACK=1 once the Space is repaired.
    if os.environ.get("USE_V1_FALLBACK", "0") == "1" and _provider_ready("v1"):
        try:
            full = (system + "\n\n" + prompt) if system else prompt
            return _call_surrogate_v1(full, timeout=max(timeout, 60))
        except Exception as e:
            _cooldown("v1", 60)
            last_err = f"surrogate-v1: {e} (after {last_err})"

    # Last absolute resort: retry HF Router Ling once with extended timeout
    # so we never drop a cycle just because one provider had a hiccup.
    try:
        return _hf_inference(messages, max_tokens, max(timeout, 60))
    except Exception as e:
        last_err = f"hf-final: {e} (after {last_err})"

    raise RuntimeError(
        f"all LLM providers failed; last={last_err}; "
        f"cooldowns: {sorted([k for k,v in _PROVIDER_COOLDOWN.items() if v > time.time()])}"
    )


# Top-tier reasoning models — used for DECISION GATES (BD verdicts, release
# approval, architecture). Per user directive 2026-05-02: "ให้ agent model
# ที่ resioning ดีๆ ตัวใหญ่กว่า เป็นคนตัดสินใจ ไม่ต้องมี human in the loop"
# We force-route through the strongest available providers ONLY — no fast-path
# fallback to 8B models, no surrogate-1 v1 — so decisions reflect real reasoning.
#
# Model names refreshed 2026-05-02:
#   - Chutes: 'deepseek-ai/DeepSeek-V3' → 'DeepSeek-V3.2-TEE' (renamed
#     after V3 EOL); added DeepSeek-R1 + Qwen3.5-397B for diversity.
#   - xAI: removed — 'grok-2-1212' deprecated and tenant has no credits
#     ('newly created team doesn't have any credits or licenses').
_STRONG_CHAIN = [
    # Provider, URL, env-key, model — ordered by reasoning quality / TPD.
    #
    # Chutes REMOVED 2026-05-02 — account balance $0.0, every call returns
    # 'Quota exceeded'. Re-add only if user funds the account (which would
    # break the free-tier-only policy — see docs/free-tier-only-policy.md).
    ("SambaNova-Llama3.3-70B",    "https://api.sambanova.ai/v1/chat/completions",
     "SAMBANOVA_API_KEY",         "Meta-Llama-3.3-70B-Instruct"),
    ("Groq-Llama3.3-70B",         "https://api.groq.com/openai/v1/chat/completions",
     "GROQ_API_KEY",              "llama-3.3-70b-versatile"),
    ("NVIDIA-Llama3.3-70B",       "https://integrate.api.nvidia.com/v1/chat/completions",
     "NVIDIA_NIM_API_KEY",        "meta/llama-3.3-70b-instruct"),
    ("OpenRouter-Llama3.3-70B",   "https://openrouter.ai/api/v1/chat/completions",
     "OPENROUTER_API_KEY",        "meta-llama/llama-3.3-70b-instruct:free"),
    # Mid-tier additions: when the 70B class is rate-limited, these still
    # provide reasonable decision quality. Cerebras + Kimi.
    # Cerebras 2026-05-03: llama-3.3-70b retired → qwen-3-235b is biggest
    # free model. gpt-oss-120b kept as separate rate-limit bucket.
    ("Cerebras-Qwen3-235B",       "https://api.cerebras.ai/v1/chat/completions",
     "CEREBRAS_API_KEY",          "qwen-3-235b-a22b-instruct-2507"),
    ("Cerebras-GPT-OSS-120B",     "https://api.cerebras.ai/v1/chat/completions",
     "CEREBRAS_API_KEY",          "gpt-oss-120b"),
    # DeepSeek native (added 2026-05-03) — V3.2 + R1 for top-tier reasoning.
    # Free signup credit. Cost-guard daemon catches any overage; user
    # rotates accounts when credit drops.
    ("DeepSeek-V3",               "https://api.deepseek.com/v1/chat/completions",
     "DEEPSEEK_API_KEY",          "deepseek-chat"),
    ("DeepSeek-R1",               "https://api.deepseek.com/v1/chat/completions",
     "DEEPSEEK_API_KEY",          "deepseek-reasoner"),
    # Together.ai trial (added 2026-05-03) — Llama-3.3-70B-Free + Qwen2.5-72B
    # are on always-free tier. $1 trial credit covers other Turbo models.
    ("Together-Llama3.3-70B-Free", "https://api.together.xyz/v1/chat/completions",
     "TOGETHER_API_KEY",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
    ("Together-Qwen2.5-72B",       "https://api.together.xyz/v1/chat/completions",
     "TOGETHER_API_KEY",          "Qwen/Qwen2.5-72B-Instruct-Turbo"),
    # Kimi removed (auth fails). Replaced with HF Router Ling-2.6-1T (free).
]


def call_llm_strong(prompt: str, system: str = "", max_tokens: int = 2000,
                    timeout: int = 60, allow_degrade: bool = False) -> str:
    """Decision-grade LLM call — top-tier reasoning models only.

    Use for BD verdicts, release approvals, root-cause analysis. Skips
    fast-path 8B + surrogate-1 v1.

    Per-provider cooldown shared with call_llm — if Groq is rate-limited
    here, call_llm also skips it. Auto-fallthrough on 429/402.

    `allow_degrade=True`: if every strong provider is in cooldown, fall
    through to standard call_llm (which has CF-AI 8B + HF Inference + v1).
    """
    # 2026-05-08 — fast-3 prepended even for STRONG calls (Gemini Flash and
    # NVIDIA 70B are decision-grade; Groq 8B is borderline but valuable when
    # 70B-class providers are quota-exhausted).
    fast = _fast_3_provider_attempt(prompt, system, max_tokens)
    if fast:
        return fast
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system[:8000]})
    messages.append({"role": "user", "content": prompt[:16000]})
    errors: list[str] = []
    for name, url, env_key, model in _STRONG_CHAIN:
        if not _provider_ready(name):
            errors.append(f"{name}: cooldown")
            continue
        key = (os.environ.get(env_key)
               or (os.environ.get("XAI_API_KEY") if env_key == "GROK_API_KEY" else None)
               or (os.environ.get("NVIDIA_API_KEY") if env_key == "NVIDIA_NIM_API_KEY" else None))
        if not key:
            errors.append(f"{name}: no key")
            continue
        body = {"model": model, "messages": messages,
                "max_tokens": max_tokens, "temperature": 0.2}
        # ── Portkey passthrough wrap (free tier, decision calls only) ─────
        # call_llm_strong = ~100-200/day = within Portkey free 10K/mo.
        # Get unified observability dashboard + trace IDs for audit.
        # If PORTKEY_API_KEY unset, falls back to direct provider call.
        pk = os.environ.get("PORTKEY_API_KEY", "")
        portkey_provider_map = {
            "SAMBANOVA_API_KEY": "sambanova",
            "GROQ_API_KEY": "groq",
            "NVIDIA_NIM_API_KEY": "nvidia",
            "OPENROUTER_API_KEY": "openrouter",
            "CEREBRAS_API_KEY": "cerebras",
            "DEEPSEEK_API_KEY": "deepseek",
            "TOGETHER_API_KEY": "together-ai",
        }
        prov_slug = portkey_provider_map.get(env_key, "")
        if pk and prov_slug:
            req_url = "https://api.portkey.ai/v1/chat/completions"
            req_headers = {
                "Authorization": f"Bearer {key}",
                "x-portkey-api-key": pk,
                "x-portkey-provider": prov_slug,
                "x-portkey-trace-id": f"axentx-{int(time.time())}-{name}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            }
        else:
            req_url = url
            req_headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            }
        req = urllib.request.Request(
            req_url, data=json.dumps(body).encode(), headers=req_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                _cooldown_clear(name)
                return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 402:
                _cooldown(name, _COOLDOWN_PAYMENT)
            elif e.code in (401, 403):
                _cooldown(name, _COOLDOWN_AUTH)
            elif e.code == 429:
                ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                _cooldown(name, int(ra) if (ra and ra.isdigit()) else _COOLDOWN_DEFAULT)
            elif 500 <= e.code < 600:
                _cooldown(name, _COOLDOWN_5XX)
            try:
                detail = e.read().decode()[:120]
            except Exception:
                detail = ""
            errors.append(f"{name}: HTTP {e.code} {detail}")
        except Exception as e:
            _cooldown(name, 60)
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:60]}")
    if allow_degrade:
        try:
            return call_llm(prompt, system, max_tokens, timeout)
        except Exception as e:
            errors.append(f"degraded-call_llm: {e}")
    raise RuntimeError(
        f"call_llm_strong: all {len(errors)} strong providers failed | "
        + " | ".join(errors[:6])
    )


def synthesize(prompt: str, system: str = "", n_attempts: int = 3,
               max_tokens: int = 1500, timeout: int = 30) -> str:
    """Generate N candidates, then call once more to synthesize the best.
    Quality > raw call_llm at the cost of N+1 LLM credits.

    If all N candidates fail (every provider rate-limited), we degrade to a
    single call_llm with a longer timeout instead of raising. Better a
    single-attempt answer than nothing — pipeline keeps flowing.
    """
    if n_attempts < 2:
        return call_llm(prompt, system, max_tokens, timeout)
    cands: list[str] = []
    last_exc: Exception | None = None
    for _ in range(n_attempts):
        try:
            cands.append(call_llm(prompt, system, max_tokens, timeout))
        except Exception as e:
            last_exc = e
            continue
    if not cands:
        # Degraded path — try one more time with extended timeout. If THIS
        # also fails, surface the original failure so debug logs stay clear.
        try:
            return call_llm(prompt, system, max_tokens, max(timeout * 2, 60))
        except Exception:
            raise RuntimeError(
                f"synthesize: no candidate succeeded; last={last_exc}"
            )
    if len(cands) == 1:
        return cands[0]
    sp = ("Synthesize the best parts of multiple AI proposals. Combine the "
          "strongest insights into ONE final answer. Resolve contradictions in "
          "favor of correctness + concrete actionability.\n\n" +
          "\n\n---\n\n".join(f"Candidate {i+1}:\n{c}" for i, c in enumerate(cands)))
    try:
        return call_llm(sp, "", max_tokens, timeout)
    except Exception:
        # Synthesis call itself rate-limited — return the first candidate
        # rather than discarding all 2-3 successful generations.
        return cands[0]




def rag_query(question: str, top_k: int = 5, kind: str | None = None) -> str:
    """RAG retrieval over the surrogate-1-rag Vectorize index.

    Returns a formatted block of top_k matches (source path + first 200 chars
    of the chunk). Agents prepend this to their LLM prompts to recall past
    decisions / lessons / skills / knowledge before generating new output.

    Falls through to empty string on any failure (CF outage, missing token,
    cold index) — RAG is augmentation, never a hard dependency.
    """
    import json as _j, urllib.request as _u, urllib.error as _ue
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not tok or not acct:
        return ""
    try:
        # 1. embed the question
        emb_req = _u.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/baai/bge-base-en-v1.5",
            data=_j.dumps({"text": [question[:500]]}).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        with _u.urlopen(emb_req, timeout=15) as r:
            emb = _j.loads(r.read())
        if not emb.get("success"): return ""
        qvec = emb["result"]["data"][0]

        # 2. query Vectorize
        q_body = {"vector": qvec, "topK": top_k, "returnMetadata": "all", "returnValues": False}
        if kind:
            q_body["filter"] = {"kind": kind}
        q_req = _u.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{acct}/vectorize/v2/indexes/surrogate-1-rag/query",
            data=_j.dumps(q_body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        with _u.urlopen(q_req, timeout=15) as r:
            q = _j.loads(r.read())
        if not q.get("success"): return ""
        matches = q["result"]["matches"]
        if not matches: return ""

        out_lines = ["=== relevant past context (RAG) ==="]
        for m in matches:
            md = m.get("metadata") or {}
            out_lines.append(
                f"  [{m.get('score',0):.2f}] {md.get('source','?')} (kind={md.get('kind','?')})"
            )
        out_lines.append("=== end RAG ===\n")
        return "\n".join(out_lines)
    except Exception:
        return ""

def rag_top_score(question: str, kind: str | None = None) -> float:
    """Return the top-1 cosine score from Vectorize for `question`.
    Returns 0.0 on any failure / empty index — callers treat 0.0 as
    'no comparable past item, safe to proceed'. Used for dedup gates."""
    import json as _j, urllib.request as _u
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not tok or not acct:
        return 0.0
    try:
        emb_req = _u.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/baai/bge-base-en-v1.5",
            data=_j.dumps({"text": [question[:500]]}).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        with _u.urlopen(emb_req, timeout=15) as r:
            emb = _j.loads(r.read())
        if not emb.get("success"): return 0.0
        qvec = emb["result"]["data"][0]
        q_body = {"vector": qvec, "topK": 1, "returnMetadata": "all", "returnValues": False}
        if kind: q_body["filter"] = {"kind": kind}
        q_req = _u.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{acct}/vectorize/v2/indexes/surrogate-1-rag/query",
            data=_j.dumps(q_body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        with _u.urlopen(q_req, timeout=15) as r:
            q = _j.loads(r.read())
        if not q.get("success"): return 0.0
        matches = q["result"]["matches"]
        if not matches: return 0.0
        return float(matches[0].get("score") or 0.0)
    except Exception:
        return 0.0


def new_item(project: str, focus: str, prompt: str) -> dict:
    ts = datetime.datetime.utcnow()
    sid = hashlib.sha1(f"{ts.isoformat()}-{project}-{focus}".encode()).hexdigest()[:8]
    return {
        "id": f"{ts.strftime('%Y%m%d-%H%M%S')}-{project}-{focus}-{sid}",
        "project": project,
        "focus": focus,
        "stage": "dev",
        "created_at": ts.isoformat() + "Z",
        "trace_id": new_trace_id(),
        "history": [],
        "current": {"text": prompt},
    }


# ─── Cross-VM queue (D1-backed) ──────────────────────────────────────────
# User directive (2026-05-02): 'Pipeline queues ไม่ shared ... GCP-produced
# items get reviewed by Kamatera and vice-versa'.
# Strategy: dual-write to FS (cache + safety) AND D1 (cross-VM coord).
# pick_oldest claims atomically from D1; falls back to FS scan if D1
# unreachable. Disable per-stage via FS_ONLY_STAGES env var if needed.
import socket as _xvm_socket
_XVM_HOST = _xvm_socket.gethostname()

# Cross-VM coordination plane.
# 2026-05-03 migration: CF Worker free-tier (100k req/day) was exhausted by
# the 572-daemon Kam scale-up. Moved primary coordination to Supabase
# (PostgREST + RPCs, ap-southeast-1 SG) — no per-request limit on the
# free tier when hitting tables/RPCs directly. CF Worker stays online for
# the dashboard + scheduled() cron only (those don't bill against
# client-side request budget).
# 2026-05-10 SUPABASE_DISABLED kill switch
# When SUPABASE_DISABLED=1 is set, treat Supabase as completely offline.
# This is the strongest off-switch; circuit breakers + fallbacks couldn't
# stop daemons hanging on TCP half-open connections to CF-fronted Supabase
# (172.64.149.246). All _sb_request / _xvm_push / _xvm_claim short-circuit.
_SB_DISABLED = os.environ.get("SUPABASE_DISABLED", "").strip() in ("1", "true", "yes")
_SB_URL = ("" if _SB_DISABLED else
           os.environ.get("SUPABASE_URL",
                          "https://riunimyxoalicbntogbp.supabase.co"))
_SB_KEY = ("" if _SB_DISABLED else
           (os.environ.get("SUPABASE_SECRET_KEY") or
            os.environ.get("SUPABASE_SERVICE_KEY", "")))
_SB_HEADERS = {
    "apikey": _SB_KEY,
    "Authorization": f"Bearer {_SB_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}
# Legacy CF endpoint kept for fallback. Set XVM_QUEUE_URL='' to disable.
_XVM_BASE = os.environ.get("XVM_QUEUE_URL", "")
_XVM_DISABLED_STAGES = set(
    s.strip() for s in os.environ.get("FS_ONLY_STAGES", "").split(",") if s.strip()
)


# 2026-05-09 _sb_request circuit breaker
# Per-process circuit breaker for Supabase. Once we hit 5 consecutive
# failures (timeout / connection error / 5xx), skip Supabase entirely
# for 10 minutes. This stops daemons hanging in do_poll on stale TCP
# half-open connections to Supabase via Cloudflare front (172.64.149.246).
import time as _sb_time_cb
_SB_FAIL_COUNT = 0
_SB_SKIP_UNTIL = 0.0
_SB_LAST_FAIL_LOG = 0.0


def _sb_request(method: str, path: str, body: dict | list | None = None,
                timeout: int = 3) -> dict | list | None:
    """Supabase REST/RPC request with circuit breaker.

    timeout default 8 → 3s (Supabase has been unreliable since 2026-05-07).
    After 5 consecutive failures, skip for 10 min (avoids do_poll hangs).
    `path` starts with '/rest/v1/...' (e.g. '/rest/v1/rpc/claim_pipeline_item').
    """
    global _SB_FAIL_COUNT, _SB_SKIP_UNTIL, _SB_LAST_FAIL_LOG
    if not (_SB_URL and _SB_KEY):
        return None
    if _sb_time_cb.time() < _SB_SKIP_UNTIL:
        return None  # circuit open
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{_SB_URL}{path}", data=data, method=method,
            headers=_SB_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            _SB_FAIL_COUNT = 0  # reset on success
            return json.loads(raw) if raw else None
    except Exception:
        _SB_FAIL_COUNT += 1
        if _SB_FAIL_COUNT >= 5:
            _SB_SKIP_UNTIL = _sb_time_cb.time() + 600
            _SB_FAIL_COUNT = 0
            # Rate-limit the "circuit open" log to once per minute
            if _sb_time_cb.time() - _SB_LAST_FAIL_LOG > 60:
                _SB_LAST_FAIL_LOG = _sb_time_cb.time()
                # Can't log() here (no role context), but don't silently
                # swallow either — print to stderr where systemd captures it
                import sys as _sys
                print("[axentx_pipeline] SB circuit breaker OPEN (10min)",
                      file=_sys.stderr, flush=True)
        return None


def _xvm_post(path: str, body: dict, timeout: int = 8) -> dict | None:
    """Coordinator (axentx-coordinator) HTTP plane. 2026-05-23 update:
    self-hosted on Kam2 at $XVM_QUEUE_URL with Bearer-token auth via
    $COORDINATOR_TOKEN. Replaces both Supabase and the CF Worker plan.
    """
    if not _XVM_BASE:
        return None
    headers = {"Content-Type": "application/json", "User-Agent": UA_BROWSER}
    tok = os.environ.get("COORDINATOR_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        req = urllib.request.Request(
            f"{_XVM_BASE}{path}", data=json.dumps(body).encode(), method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _xvm_push(item: dict, stage: str) -> None:
    """Mirror item to coordination plane so other VMs can claim it.
    Tries Supabase first (unlimited rate-limit on PostgREST direct), falls
    back to CF Worker if Supabase unconfigured."""
    if stage in _XVM_DISABLED_STAGES:
        return
    if _SB_URL and _SB_KEY:
        # Supabase: upsert into pipeline_items table. Use Prefer: resolution=
        # merge-duplicates so re-pushes (e.g. retry after crash) don't fail.
        try:
            req = urllib.request.Request(
                f"{_SB_URL}/rest/v1/pipeline_items",
                data=json.dumps({
                    "id": item.get("id", ""),
                    "stage": stage,
                    "project": item.get("project", ""),
                    "focus": item.get("focus", ""),
                    "payload": item,
                }).encode(),
                method="POST",
                headers={**_SB_HEADERS,
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            urllib.request.urlopen(req, timeout=8).read()
            return
        except Exception:
            pass  # fall through to CF
    _xvm_post("/queue/push", {
        "id": item.get("id", ""),
        "stage": stage,
        "project": item.get("project", ""),
        "focus": item.get("focus", ""),
        "payload": item,
    })


def _xvm_claim(stage: str) -> dict | None:
    """Atomically claim oldest unclaimed item across all VMs.
    Supabase RPC `claim_pipeline_item` does the SELECT FOR UPDATE SKIP LOCKED
    + UPDATE...RETURNING dance in a single round-trip. Falls back to CF
    Worker /queue/claim if Supabase unconfigured."""
    if stage in _XVM_DISABLED_STAGES:
        return None
    if _SB_URL and _SB_KEY:
        r = _sb_request("POST", "/rest/v1/rpc/claim_pipeline_item", {
            "p_stage": stage, "p_claimer": _XVM_HOST, "p_ttl": 600,
        })
        # RPC returns a record (dict) or None when nothing claimed
        if isinstance(r, dict) and r.get("id"):
            return r
        if isinstance(r, list) and r and r[0].get("id"):
            return r[0]
        return None
    r = _xvm_post("/queue/claim", {
        "stage": stage, "claimer": _XVM_HOST, "ttl_sec": 600,
    })
    if not r:
        return None
    # axentx-coordinator returns the item dict directly (with payload key);
    # legacy CF Worker wrapped it in {"item": {...}}. Support both shapes.
    if r.get("id"):
        return r
    return r.get("item") or None


def write_item(item: dict, stage: str) -> Path:
    """Write to FS queue (cache) AND D1 (cross-VM coordination)."""
    # Defensive mkdir on every write — protects against the queue dir being
    # deleted out from under us at runtime (observed 2026-05-02).
    QUEUES[stage].mkdir(parents=True, exist_ok=True)
    path = QUEUES[stage] / f"{item['id']}.json"
    item["stage"] = stage
    path.write_text(json.dumps(item, indent=2))
    # Mirror to D1 (best-effort). If CF is unreachable, FS still works.
    _xvm_push(item, stage)
    return path



def _emit_demand_signal(stage: str) -> None:
    """Worker called pick_oldest() but found no work — signal hungry to
    demand-amplifier. Best-effort, never raises."""
    try:
        from axentx_shared import kv_set
        kv_set(f"demand.{stage}", {
            "hungry": True,
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": _XVM_HOST,
        })
    except Exception:
        pass


def pick_oldest(stage: str) -> tuple[Path, dict] | None:
    """Atomically claim oldest item across all VMs.

    Order:
      1. Try D1 atomic claim (UPDATE...RETURNING). If returns an item,
         materialize it on local FS and return.
      2. Fall back to local FS scan (legacy / D1-down behavior).

    The materialized FS path lets advance() / fail() use the same
    src_path.unlink() semantics regardless of source.
    """
    QUEUES[stage].mkdir(parents=True, exist_ok=True)
    # 1. D1 claim
    claimed = _xvm_claim(stage)
    if claimed:
        payload = claimed.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        item_id = claimed.get("id") or payload.get("id")
        if item_id and isinstance(payload, dict) and payload:
            # Make sure callers can use item["id"] safely. The D1 row stores
            # id outside the payload blob, so the rehydrated payload may not
            # have it — copy it back in. Same for stage/project/focus when
            # we have them on the row.
            payload.setdefault("id", item_id)
            for k in ("stage", "project", "focus"):
                v = claimed.get(k)
                if v and not payload.get(k):
                    payload[k] = v
            path = QUEUES[stage] / f"{item_id}.json"
            try:
                path.write_text(json.dumps(payload, indent=2))
                return path, payload
            except Exception:
                pass
    # 2. FS fallback with atomic claim
    # 2026-05-05: Fix race condition. Was: glob() → read() against same
    # oldest file from multiple workers → 5x duplicate work → 5x LLM
    # waste → rate limits hit 5x faster. Now: rename(.json → .claimed-)
    # is atomic on POSIX. Winner reads + processes; losers retry.
    # Self-heal: sweep stale .claimed-* (>900s) back to .json so a
    # crashed worker doesn't lose items forever.
    import os as _os, time as _t, re as _re
    _now = _t.time()
    for _c in QUEUES[stage].glob("*.claimed-*.json"):
        try:
            if _now - _c.stat().st_mtime > 900:
                _orig_name = _re.sub(r"\.claimed-\d+-\d+\.json$", ".json", _c.name)
                _orig = _c.with_name(_orig_name)
                if not _orig.exists():
                    _c.rename(_orig)
        except Exception:
            pass
    # 2026-05-22 project-fair claim: shuffle within oldest tier so dev pool
    # spreads across projects (was: strict FIFO → all devs rushed one project).
    import random as _rnd_claim
    _all = sorted(
        (p for p in QUEUES[stage].glob("*.json") if ".claimed-" not in p.name),
        key=lambda p: p.stat().st_mtime,
    )
    # 2026-05-22 bigger shuffle window — 80 was too small, airship/etc never picked
    # 2026-05-23 per-project diversity cap (was: top-2000 shuffle = 97% surrogate-1)
    from collections import defaultdict as _dd
    import re as _re_proj
    _per_proj = _dd(list)
    _PROJ_CAP = 100
    for _p in _all:
        _m = _re_proj.match(r'\d+-\d+-([a-z][a-z0-9-]+?)-E\d', _p.name)
        _proj = _m.group(1) if _m else 'unknown'
        if len(_per_proj[_proj]) < _PROJ_CAP:
            _per_proj[_proj].append(_p)
    _tier = []
    for _flist in _per_proj.values():
        _tier.extend(_flist)
    _rnd_claim.shuffle(_tier)
    _tier_set = set(_tier)
    _rest = [p for p in _all if p not in _tier_set]
    files = _tier + _rest
    pid = _os.getpid()
    for p in files:
        claimed = p.with_name(f"{p.stem}.claimed-{pid}-{int(_t.time()*1000)}.json")
        try:
            _os.rename(str(p), str(claimed))
        except (FileNotFoundError, OSError):
            continue  # another worker won the race
        try:
            return claimed, json.loads(claimed.read_text())
        except Exception:
            try:
                claimed.rename(claimed.with_suffix(".corrupt"))
            except Exception:
                pass
            continue
    _emit_demand_signal(stage)
    return None


def advance(item: dict, src_path: Path, next_stage: str,
            actor: str, output: str) -> Path:
    """Move item from current stage to next, append history entry.
    Preserves trace_id + discovery_id once set (never overwrites).
    Writes to D1 + FS atomically via write_item()."""
    if not item.get("trace_id"):
        item["trace_id"] = new_trace_id()
    item.setdefault("history", []).append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": output[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    # Bug fix 2026-05-04: items from new discovery streams (medium-crawler,
    # hn-pain-stream, ih-stream, ph-stream, yc-rfs, etc.) don't include a
    # 'current' key — caused KeyError in pain-validator. Setdefault before
    # mutating so any item shape is safe.
    if "current" not in item or not isinstance(item.get("current"), dict):
        item["current"] = {"text": ""}
    item["current"]["text"] = output[:6000]
    src_path.unlink(missing_ok=True)
    # Advance — repoint the row to the new stage atomically. write_item
    # below also re-pushes for safety, but the explicit advance avoids a
    # window where the item appears in TWO stages.
    if _SB_URL and _SB_KEY:
        _sb_request("POST", "/rest/v1/rpc/advance_pipeline_item", {
            "p_id": item.get("id", ""),
            "p_next_stage": next_stage,
            "p_payload": item,
        })
    else:
        _xvm_post("/queue/advance", {
            "id": item.get("id", ""),
            "next_stage": next_stage,
            "payload": item,
        })
    return write_item(item, next_stage)


def fail(item: dict, src_path: Path, actor: str, err: str) -> None:
    """Mark item as failed.

    Bug fix 2026-05-04: LLM-exhaustion failures used to send items
    straight to 'done' stage (with output 'FAILED: LLM failed: ...')
    which means once the LLM chain recovered, those items were dead
    forever. User noticed: ashirapit/* repos with 0 KB despite items
    being marked done.

    New behavior: detect transient LLM failures + retry up to N
    attempts in the SAME stage (so a different daemon, after cooldown,
    can pick it up and try again). Real terminal failures still go to
    'done'.
    """
    err_low = err.lower()
    is_transient_llm = (
        "llm failed" in err_low
        or "all llm providers failed" in err_low
        or "no candidate succeeded" in err_low
        or "http error 402" in err_low
        or "http error 429" in err_low
        or "http error 5" in err_low
    )
    cur_stage = item.get("stage")
    attempts = item.get("retry_attempts", 0) + 1
    item["retry_attempts"] = attempts
    MAX_RETRY = 8

    item.setdefault("history", []).append({
        "stage": cur_stage,
        "actor": actor,
        "output": (f"RETRY ({attempts}/{MAX_RETRY}): {err}"
                   if is_transient_llm and attempts < MAX_RETRY
                   else f"FAILED: {err}"),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    src_path.unlink(missing_ok=True)

    if is_transient_llm and attempts < MAX_RETRY and cur_stage:
        # Put item back in the SAME queue — another daemon will retry
        # after our cooldowns expire (typically 30s-10min).
        write_item(item, cur_stage)
    else:
        write_item(item, "done")


# Stage SLOs (seconds) — if a single do_one cycle exceeds this, we log a warn.
# Keys are the role name daemon_loop is launched with. Tune as cost/quality changes.
# 2026-05-09 SLO realism bump
STAGE_SLO_SEC = {
    "research": 60,    # external HTTP — fast
    "bd": 90,          # LLM call (was 45 → too tight, p90 60s+)
    "design": 90,      # LLM-heavy (was 50)
    "business": 90,    # LLM-heavy (was 50)
    "marketing": 90,   # LLM-heavy (was 60)
    "prd": 120,        # LLM-heavy (was 90)
    "dev": 240,        # multi-step refine cycles (was 60 → wildly off, p90 300s+)
    "reviewer": 180,   # LLM-heavy review (was 45 → 72 breaches/h, observed median 82s)
    "qa": 60,          # LLM testing (was 30)
    "commit": 60,      # git push + LLM commit-msg (was 20)
}

# Hibernation: after this many consecutive idle cycles with no work, sleep
# for HIBERNATE_MULT × poll_sec to ease CPU on a quiet pipeline. Reset on
# any cycle that did work.
HIBERNATE_AFTER = int(os.environ.get("HIBERNATE_AFTER", "12"))
HIBERNATE_MULT = int(os.environ.get("HIBERNATE_MULT", "5"))


# ═══════════════════════════════════════════════════════════════════════
# Universal portfolio access — added 2026-05-04 after user feedback:
# 'ทุก agent จาก ทุกที่ รู้ไหม ว่า ตอนนี้มี project อะไรบ้าง — มันต้องรู้
#  ร่วมกันหมดนะ โดยเฉพาะ BD'.
#
# ANY daemon can call get_portfolio() to read current product list from
# shared_kv["bd.portfolio"] (refreshed every 30 min by portfolio-syncer).
# Cached locally for 60s to avoid Supabase hammer.
# ═══════════════════════════════════════════════════════════════════════

_portfolio_cache: dict = {"ts": 0, "products": {}}


def get_portfolio() -> dict[str, str]:
    """Return current axentx product portfolio as {slug: description}.
    Reads shared_kv["bd.portfolio"]. 60s in-process cache. Falls back to
    a hard-coded base if shared_kv unavailable. Every agent should call
    this before LLM verdicts that depend on 'which products exist'."""
    now_t = time.time()
    if now_t - _portfolio_cache["ts"] < 60 and _portfolio_cache["products"]:
        return dict(_portfolio_cache["products"])
    products: dict[str, str] = {
        # base 5 paid products + surrogate
        "Costinel":  "AWS cost analytics + anomaly detection (SREs/finops)",
        "vanguard":  "Cloud security posture management / CSPM",
        "airship":   "IaC + multi-cloud DevSecOps unified",
        "workio":    "Workflow automation (Zapier for eng teams)",
        "surrogate": "Autonomous AI dev agent (this stack)",
    }
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from axentx_shared import kv_get
        v = kv_get("bd.portfolio") or {}
        live = (v.get("products") or {}) if isinstance(v, dict) else {}
        if isinstance(live, dict):
            for slug, desc in live.items():
                if slug and isinstance(desc, str) and desc.strip():
                    products[slug] = desc.strip()
    except Exception:
        pass
    _portfolio_cache["ts"] = now_t
    _portfolio_cache["products"] = products
    return products


def get_portfolio_block(label: str = "Active axentx products") -> str:
    """Render portfolio as a numbered list block for LLM prompts.
    Drop-in for any agent's system prompt — bd, design, architect, prd,
    dev, qa, review, pitch, business-synthesis all use the same source."""
    products = get_portfolio()
    lines = [f"{label} (every agent must respect — never duplicate, "
             "always extend when overlap):", ""]
    for i, (slug, desc) in enumerate(sorted(products.items()), 1):
        lines.append(f"{i}. {slug}: {desc[:320]}")
    return "\n".join(lines)


def daemon_loop(role: str, poll_sec: int, work_fn) -> None:
    """Generic daemon main — never returns. Polls input queue, runs work_fn.
    OOM-hardened: explicit gc + RSS check + bail-out before kill.

    Cross-host sync (added 2026-05-04 — every agent broadcasts now):
    User feedback: 'มันต้องแชร์กับ agent ทุกตัว และทุกที่ ... experience
    เพิ่มขึ้นเรื่อยๆ เก่งขึ้นเรื่อยๆ'.
    - shared_kv["agent.heartbeat.<role>.<host>"] updated EVERY cycle so all
      agents on all hosts see who's alive + what they're doing.
    - shared_memory entry posted EVERY 25 productive cycles (or every 5min
      idle) so the agent's recent activity is visible to peers.
    - Reads peer-learnings on startup: last 5 memory entries from same role
      on other hosts → daemon can use them to skip already-tried approaches.
    Imported lazily: a daemon without Supabase env still runs (sync no-ops).
    """
    import gc
    import resource
    import signal
    import socket as _socket
    _HOST = _socket.gethostname()

    # Lazy-import shared_kv/memory helpers
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from axentx_shared import kv_set as _kv_set
        from axentx_shared import memory_log as _mem_log
        from axentx_shared import kv_get as _kv_get
        _SYNC = True
    except Exception:
        _SYNC = False
        _kv_set = lambda *a, **k: None  # noqa
        _kv_get = lambda *a, **k: None  # noqa
        _mem_log = lambda *a, **k: None  # noqa

    def _broadcast_heartbeat(state: str, cycle_n: int, n_processed: int,
                             extra: dict | None = None) -> None:
        if not _SYNC:
            return
        try:
            payload = {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "host": _HOST,
                "role": role,
                "state": state,
                "cycle_n": cycle_n,
                "processed": n_processed,
            }
            if extra:
                payload.update(extra)
            _kv_set(f"agent.heartbeat.{role}.{_HOST}", payload)
        except Exception:
            pass

    def _broadcast_experience(kind: str, title: str, body: str = "") -> None:
        if not _SYNC:
            return
        try:
            _mem_log(role, kind, title, body=body[:1500],
                     tags=["agent-experience", role, _HOST])
        except Exception:
            pass

    def _read_peer_learnings(limit: int = 5) -> list[dict]:
        """Last N experiences from the same role on OTHER hosts. Used to
        avoid redoing failed approaches. Best-effort, returns []."""
        if not _SYNC:
            return []
        try:
            import urllib.request, urllib.parse, json as _json
            sb_url = os.environ.get("SUPABASE_URL", "")
            sb_key = (os.environ.get("SUPABASE_SECRET_KEY")
                      or os.environ.get("SUPABASE_SERVICE_KEY", ""))
            if not (sb_url and sb_key):
                return []
            qs = urllib.parse.urlencode({
                "actor": f"eq.{role}",
                "host": f"neq.{_HOST}",
                "select": "kind,title,body,host,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            })
            req = urllib.request.Request(
                f"{sb_url}/rest/v1/shared_memory?{qs}",
                headers={"apikey": sb_key,
                         "Authorization": f"Bearer {sb_key}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return _json.loads(r.read())
        except Exception:
            return []

    # Read peer learnings once at startup → stash on a module-level dict
    # the daemon can `from axentx_pipeline import PEER_LEARNINGS` to consult.
    try:
        global PEER_LEARNINGS
        PEER_LEARNINGS = _read_peer_learnings(limit=5)
        if PEER_LEARNINGS:
            log(role, f"  ↳ {len(PEER_LEARNINGS)} peer-learning(s) loaded "
                      f"from other hosts")
    except Exception:
        pass

    # Heartbeat — best-effort, never breaks the daemon. Imported lazily so a
    # bot without CF creds still runs (heartbeat just no-ops).
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "agent_heartbeat",
            str(Path(__file__).parent / "agent-heartbeat.py"),
        )
        _hb = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_hb)
        _hb.start_heartbeat(role, initial_state="starting")
    except Exception:
        _hb = None  # heartbeat unavailable — keep going

    def shutdown(*_):
        log(role, "shutdown signal")
        if _hb is not None:
            try:
                _hb.stop_heartbeat()
            except Exception:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # MemoryMax in systemd is 64M; we self-restart at 48M to avoid hard kill
    SOFT_RSS_KB = int(os.environ.get("DAEMON_SOFT_RSS_KB", "49152"))  # 48 MB
    # Match SLO on the role's primary key (e.g. "research-1" → "research")
    slo_key = role.split("-", 1)[0]
    slo_sec = STAGE_SLO_SEC.get(slo_key)
    log(role, f"start — poll every {poll_sec}s, RSS soft cap {SOFT_RSS_KB} KB"
              f"{f', SLO {slo_sec}s' if slo_sec else ''}")
    n_processed = 0
    n_idle = 0
    cycle_n = 0
    _last_milestone_log_n = 0
    while True:
        cycle_n += 1
        if _hb is not None:
            try:
                _hb.heartbeat(role, state="working", task=f"cycle#{cycle_n}",
                              cycle_n=cycle_n)
            except Exception:
                pass
        # Cross-host heartbeat — visible to peers in shared_kv.
        _broadcast_heartbeat("working", cycle_n, n_processed)
        t0 = time.monotonic()
        try:
            did_work = work_fn()
        except Exception as e:
            log(role, f"⚠ exception: {type(e).__name__}: {e}")
            did_work = False
            if _hb is not None:
                try:
                    _hb.heartbeat(role, state="error",
                                  task=f"cycle#{cycle_n}",
                                  error=f"{type(e).__name__}: {str(e)[:100]}")
                except Exception:
                    pass
        elapsed = time.monotonic() - t0

        # SLO breach warning — only when work happened (idle cycle is fast/short)
        if did_work and slo_sec and elapsed > slo_sec:
            log(role, f"⚠ SLO breach: cycle took {elapsed:.1f}s > {slo_sec}s",
                level="warn", elapsed=round(elapsed, 1), slo=slo_sec)

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
            if _hb is not None:
                try:
                    _hb.heartbeat(role, state="idle",
                                  task=f"done cycle#{cycle_n}",
                                  cycle_n=cycle_n)
                except Exception:
                    pass
            # Cross-host: push idle-state heartbeat after work
            _broadcast_heartbeat("idle", cycle_n, n_processed,
                                 extra={"last_cycle_sec": round(elapsed, 2)})
            # Milestone broadcast — every 25 productive cycles, log experience
            # so peers can see this agent is making progress + what it learned.
            if n_processed - _last_milestone_log_n >= 25:
                _broadcast_experience(
                    "milestone",
                    f"{role} processed {n_processed} items "
                    f"(this process, cycle#{cycle_n})",
                    body=f"host={_HOST} role={role} cycle={cycle_n} "
                         f"avg_cycle_sec={round(elapsed, 2)}")
                _last_milestone_log_n = n_processed
            time.sleep(2)
        else:
            n_idle += 1
            if n_idle % 20 == 1:
                log(role, f"idle (processed={n_processed} cycles, RSS={rss_kb} KB)")
            if _hb is not None:
                try:
                    _hb.heartbeat(role, state="idle",
                                  task=f"idle×{n_idle}",
                                  cycle_n=cycle_n)
                except Exception:
                    pass
            _broadcast_heartbeat("idle", cycle_n, n_processed,
                                 extra={"idle_streak": n_idle})
            # Hibernate when persistently idle — saves CPU on a quiet pipeline.
            sleep_sec = poll_sec * HIBERNATE_MULT if n_idle >= HIBERNATE_AFTER else poll_sec
            time.sleep(sleep_sec)
