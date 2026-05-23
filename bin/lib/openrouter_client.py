"""OpenRouter client — free-first then paid tiers.

Tiers (per Ashira 2026-04-19):
  FREE:    qwen, gpt-oss, llama, nemotron, glm
  CHEAP:   deepseek-v3.2, grok-4.1-fast
  PREMIUM: gpt-5.4, claude-haiku-4.5, claude-sonnet-4.6, claude-opus-4.7

Per-model cooldown tracked in ~/.surrogate/yolo/or-cooldowns.json to avoid
hammering rate-limited free models.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
COOLDOWN_PATH = Path.home() / ".surrogate" / "yolo" / "or-cooldowns.json"

FREE_MODELS = [
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
]

CHEAP_MODELS = [
    "deepseek/deepseek-v3.2",
    "x-ai/grok-4.1-fast",
]

PREMIUM_MODELS = [
    "openai/gpt-5.4",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4.6",
    "x-ai/grok-4.20",
    "anthropic/claude-opus-4.7",
]

DEFAULT_COOLDOWN_SECONDS = 60  # after 429, wait 60s before retrying this model


class ORUnavailable(Exception):
    def __init__(self, model: str, code: int, body: str):
        self.model = model
        self.code = code
        self.body = body
        super().__init__(f"OR {model}: {code} {body[:200]}")


@dataclass
class ORResponse:
    content: str
    model_requested: str
    model_served: str
    input_tokens: int = 0
    output_tokens: int = 0


def _load_cooldowns() -> dict[str, float]:
    if not COOLDOWN_PATH.exists():
        return {}
    try:
        return json.loads(COOLDOWN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cooldowns(c: dict[str, float]) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(c))


def is_on_cooldown(model: str) -> bool:
    c = _load_cooldowns()
    return c.get(model, 0) > time.time()


def mark_cooldown(model: str, seconds: int = DEFAULT_COOLDOWN_SECONDS) -> None:
    c = _load_cooldowns()
    c[model] = time.time() + seconds
    # Prune expired entries
    c = {k: v for k, v in c.items() if v > time.time()}
    _save_cooldowns(c)


def call_openrouter(
    model: str,
    messages: list[dict],
    max_tokens: int = 4000,
    system: Optional[str] = None,
    timeout: int = 120,
) -> ORResponse:
    """Call OpenRouter directly. Raises ORUnavailable on error."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        # Try loading from .env (accepts both `KEY=val` and `export KEY=val` formats)
        env_file = Path.home() / ".surrogate" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                s = line.strip()
                if s.startswith("export "):
                    s = s[len("export "):].lstrip()
                if s.startswith("OPENROUTER_API_KEY="):
                    api_key = s.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        raise ORUnavailable(model, 0, "OPENROUTER_API_KEY not set")

    body_msgs = list(messages)
    if system:
        body_msgs = [{"role": "system", "content": system}] + body_msgs

    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": body_msgs,
    }).encode()

    req = urllib.request.Request(
        OR_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/Ashira/axentx",
            "X-Title": "axentx-smart-dispatcher",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            if "choices" not in data:
                raise ORUnavailable(model, 0, str(data)[:200])
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage", {})
            return ORResponse(
                content=content,
                model_requested=model,
                model_served=data.get("model", model),
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        # 429 or 503 → mark cooldown
        if e.code in (429, 503, 502):
            mark_cooldown(model)
        raise ORUnavailable(model, e.code, body)
    except Exception as e:  # network errors
        raise ORUnavailable(model, 0, str(e))


def pick_free() -> Optional[str]:
    """First free model not on cooldown."""
    for m in FREE_MODELS:
        if not is_on_cooldown(m):
            return m
    return None


def pick_cheap() -> Optional[str]:
    for m in CHEAP_MODELS:
        if not is_on_cooldown(m):
            return m
    return None


def pick_premium() -> Optional[str]:
    for m in PREMIUM_MODELS:
        if not is_on_cooldown(m):
            return m
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "pick":
        print(f"free:    {pick_free()}")
        print(f"cheap:   {pick_cheap()}")
        print(f"premium: {pick_premium()}")
    else:
        m = pick_free() or pick_cheap() or pick_premium()
        q = sys.argv[1] if len(sys.argv) > 1 else "say OK"
        r = call_openrouter(m, [{"role": "user", "content": q}], max_tokens=30)
        print(f"[{r.model_served}] {r.content[:100]}")
