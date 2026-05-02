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
    # New product-discovery pipeline:
    # research → validator → bd → design → business → marketing → prd → dev
    "research":  SHARED / "research-queue",
    "validator": SHARED / "validator-queue",
    "bd":        SHARED / "bd-queue",
    "design":    SHARED / "design-queue",
    "business":  SHARED / "business-queue",
    "marketing": SHARED / "marketing-queue",
    "prd":       SHARED / "prd-queue",
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
_COOLDOWN_DEFAULT = 120.0    # 2 min default cooldown after rate-limit
_COOLDOWN_PAYMENT = 86400.0  # 24h after 402 (account drained, won't recover today)


def _provider_ready(name: str) -> bool:
    until = _PROVIDER_COOLDOWN.get(name, 0)
    return until <= time.time()


def _cooldown(name: str, sec: float | None = None) -> None:
    sec = sec if sec is not None else _COOLDOWN_DEFAULT
    _PROVIDER_COOLDOWN[name] = time.time() + sec


def _call_codespace_ollama(messages: list, max_tokens: int, timeout: int) -> str:
    """GH Codespace running ollama (Qwen2.5-Coder-7B 4-bit) — deepest free
    fallback that doesn't depend on any external rate-limited provider.

    URL via env CODESPACE_LLM_URL (e.g.
      https://ollama-llm-proxy-xxx-11434.app.github.dev). Codespace owner
    sets the port visibility to 'public' once via gh CLI:
      gh codespace ports visibility 11434:public -c <name>

    Codespace auto-stops after 30min idle to preserve free quota. First
    call after wake-up takes ~30-60s for container restart + model load;
    subsequent calls are 2-5s.
    """
    base = os.environ.get("CODESPACE_LLM_URL", "")
    if not base:
        raise RuntimeError("no CODESPACE_LLM_URL")
    model = os.environ.get("CODESPACE_LLM_MODEL", "qwen2.5-coder:7b-instruct-q4_K_M")
    url = base.rstrip("/") + "/v1/chat/completions"
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.3}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def _call_kamatera_ollama(messages: list, max_tokens: int, timeout: int) -> str:
    """Kamatera VM running ollama (Qwen2.5-Coder-7B 5-bit). Only reachable
    from the GCP egress IP via ufw — 2026-05-02 firewall rule. Different IP
    family from codespace (private GH range) so a single AS-wide block on
    GitHub does not also block this. Use as 2nd codespace fallback."""
    base = os.environ.get("KAMATERA_LLM_URL", "")
    if not base:
        raise RuntimeError("no KAMATERA_LLM_URL")
    model = os.environ.get("KAMATERA_LLM_MODEL", "qwen2.5-coder:7b-instruct-q5_K_M")
    url = base.rstrip("/") + "/v1/chat/completions"
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.3}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


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


def call_llm(prompt: str, system: str = "", max_tokens: int = 1500,
             timeout: int = 30) -> str:
    """Multi-provider fallback chain with per-provider cooldown.

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
    chains = [
        ("Groq",          "https://api.groq.com/openai/v1/chat/completions",
         os.environ.get("GROQ_API_KEY"), "llama-3.3-70b-versatile"),
        ("Cerebras",      "https://api.cerebras.ai/v1/chat/completions",
         os.environ.get("CEREBRAS_API_KEY"), "llama-3.3-70b"),
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
        ("GitHub-Models", "https://models.inference.ai.azure.com/chat/completions",
         os.environ.get("GITHUB_MODELS_TOKEN"), "gpt-4o-mini"),
        # Mistral free tier — separate budget pool from everything above.
        ("Mistral",       "https://api.mistral.ai/v1/chat/completions",
         os.environ.get("MISTRAL_API_KEY"), "mistral-small-latest"),
    ]
    # Skip providers in cooldown — round-robin to first available.
    chains_ready = [c for c in chains if c[2] and _provider_ready(c[0])]
    last_err = None
    payload = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    for name, url, key, model in chains_ready:
        body = dict(payload, model=model)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 402:
                _cooldown(name, _COOLDOWN_PAYMENT)
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

    # Codespace ollama (Qwen2.5-Coder-7B 4-bit) — IP-distinct from CF/GCP,
    # owns its own GPU-less budget. Used when everything above hits limits.
    if _provider_ready("Codespace-LLM") and os.environ.get("CODESPACE_LLM_URL"):
        try:
            return _call_codespace_ollama(messages, max_tokens, max(timeout, 60))
        except urllib.error.HTTPError as e:
            _cooldown("Codespace-LLM", _COOLDOWN_DEFAULT)
            last_err = f"Codespace-LLM: HTTP {e.code} (after {last_err})"
        except Exception as e:
            _cooldown("Codespace-LLM", 120)
            last_err = f"Codespace-LLM: {e} (after {last_err})"

    # Kamatera ollama (Qwen2.5-Coder-7B 5-bit, slightly higher quality than
    # codespace's 4-bit). Independent VM, independent IP — used when codespace
    # is mid-restart or has hit its 24h-keep-alive eviction. Internal-only:
    # ufw on Kam allows port 11434 from GCP egress IP only.
    if _provider_ready("Kamatera-LLM") and os.environ.get("KAMATERA_LLM_URL"):
        try:
            return _call_kamatera_ollama(messages, max_tokens, max(timeout, 60))
        except urllib.error.HTTPError as e:
            _cooldown("Kamatera-LLM", _COOLDOWN_DEFAULT)
            last_err = f"Kamatera-LLM: HTTP {e.code} (after {last_err})"
        except Exception as e:
            _cooldown("Kamatera-LLM", 120)
            last_err = f"Kamatera-LLM: {e} (after {last_err})"

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
    ("Cerebras-Llama3.3-70B",     "https://api.cerebras.ai/v1/chat/completions",
     "CEREBRAS_API_KEY",          "llama-3.3-70b"),
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
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": UA_BROWSER,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
                return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 402:
                _cooldown(name, _COOLDOWN_PAYMENT)
            elif e.code == 429:
                ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                _cooldown(name, int(ra) if (ra and ra.isdigit()) else _COOLDOWN_DEFAULT)
            elif 500 <= e.code < 600:
                _cooldown(name, 60)
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
_XVM_BASE = os.environ.get(
    "XVM_QUEUE_URL", "https://surrogate-1-cursor.ashira.workers.dev"
)
_XVM_DISABLED_STAGES = set(
    s.strip() for s in os.environ.get("FS_ONLY_STAGES", "").split(",") if s.strip()
)


def _xvm_post(path: str, body: dict, timeout: int = 8) -> dict | None:
    if not _XVM_BASE:
        return None
    try:
        req = urllib.request.Request(
            f"{_XVM_BASE}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA_BROWSER},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _xvm_push(item: dict, stage: str) -> None:
    """Mirror item to D1 so other VMs can claim it."""
    if stage in _XVM_DISABLED_STAGES:
        return
    _xvm_post("/queue/push", {
        "id": item.get("id", ""),
        "stage": stage,
        "project": item.get("project", ""),
        "focus": item.get("focus", ""),
        "payload": item,
    })


def _xvm_claim(stage: str) -> dict | None:
    """Atomically claim oldest unclaimed D1 item for this host."""
    if stage in _XVM_DISABLED_STAGES:
        return None
    r = _xvm_post("/queue/claim", {
        "stage": stage, "claimer": _XVM_HOST, "ttl_sec": 600,
    })
    if not r:
        return None
    return (r.get("item") or None)


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
            path = QUEUES[stage] / f"{item_id}.json"
            try:
                path.write_text(json.dumps(payload, indent=2))
                return path, payload
            except Exception:
                pass
    # 2. FS fallback
    files = sorted(QUEUES[stage].glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            return p, json.loads(p.read_text())
        except Exception:
            p.rename(p.with_suffix(".corrupt"))
            continue
    return None


def advance(item: dict, src_path: Path, next_stage: str,
            actor: str, output: str) -> Path:
    """Move item from current stage to next, append history entry.
    Preserves trace_id + discovery_id once set (never overwrites).
    Writes to D1 + FS atomically via write_item()."""
    if not item.get("trace_id"):
        item["trace_id"] = new_trace_id()
    item["history"].append({
        "stage": item.get("stage"),
        "actor": actor,
        "output": output[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = output[:6000]
    src_path.unlink(missing_ok=True)
    # D1 advance — repoint the row to the new stage atomically. write_item
    # below also re-pushes for safety, but the explicit advance avoids a
    # window where the item appears in TWO stages.
    _xvm_post("/queue/advance", {
        "id": item.get("id", ""),
        "next_stage": next_stage,
        "payload": item,
    })
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


# Stage SLOs (seconds) — if a single do_one cycle exceeds this, we log a warn.
# Keys are the role name daemon_loop is launched with. Tune as cost/quality changes.
STAGE_SLO_SEC = {
    "research": 60,
    "bd": 45,
    "design": 50,
    "business": 50,
    "marketing": 60,
    "prd": 90,
    "dev": 60,
    "reviewer": 45,
    "qa": 30,
    "commit": 20,
}

# Hibernation: after this many consecutive idle cycles with no work, sleep
# for HIBERNATE_MULT × poll_sec to ease CPU on a quiet pipeline. Reset on
# any cycle that did work.
HIBERNATE_AFTER = int(os.environ.get("HIBERNATE_AFTER", "12"))
HIBERNATE_MULT = int(os.environ.get("HIBERNATE_MULT", "5"))


def daemon_loop(role: str, poll_sec: int, work_fn) -> None:
    """Generic daemon main — never returns. Polls input queue, runs work_fn.
    OOM-hardened: explicit gc + RSS check + bail-out before kill.
    Heartbeats automatically: every cycle posts {state,task,cycle_n} to CF KV
    so /dash/agents shows live status across the fleet."""
    import gc
    import resource
    import signal

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
    while True:
        cycle_n += 1
        if _hb is not None:
            try:
                _hb.heartbeat(role, state="working", task=f"cycle#{cycle_n}",
                              cycle_n=cycle_n)
            except Exception:
                pass
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
            # Hibernate when persistently idle — saves CPU on a quiet pipeline.
            sleep_sec = poll_sec * HIBERNATE_MULT if n_idle >= HIBERNATE_AFTER else poll_sec
            time.sleep(sleep_sec)
