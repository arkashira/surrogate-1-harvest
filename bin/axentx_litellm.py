"""axentx_litellm — thin wrapper for LiteLLM proxy.

When LITELLM_PROXY_URL is set in env, route LLM calls through it for
unified cost tracking + fallback. When not set, no-op (return None) so
caller falls back to existing chain in axentx_pipeline.call_llm.

Setup (operator action — one-time):
    docker run -d --name litellm \\
      -p 4000:4000 \\
      -e LITELLM_MASTER_KEY=sk-axentx \\
      -v /etc/litellm/config.yaml:/app/config.yaml \\
      ghcr.io/berriai/litellm:main-stable --config /app/config.yaml
    # then set LITELLM_PROXY_URL=http://<host>:4000 in env.canonical
"""
from __future__ import annotations
import json
import os
import urllib.request

LITELLM_URL = os.environ.get("LITELLM_PROXY_URL", "")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-axentx")


def call(messages: list[dict], model: str = "agent-llm",
         max_tokens: int = 1500, temperature: float = 0.3,
         timeout: int = 60) -> str | None:
    """Send chat completion through LiteLLM proxy. Returns content or None."""
    if not LITELLM_URL:
        return None
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    try:
        req = urllib.request.Request(
            f"{LITELLM_URL}/v1/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {LITELLM_KEY}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception:
        return None


__all__ = ["call", "LITELLM_URL"]
