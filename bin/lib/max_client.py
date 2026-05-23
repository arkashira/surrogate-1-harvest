"""Claude Max plan OAuth client.

Handles:
  - Read OAuth token from macOS keychain (`Claude Code-credentials`)
  - Auto-refresh before expiry (lazy, on API call)
  - Call Anthropic `/v1/messages` with OAuth Bearer
  - Parse `anthropic-ratelimit-*` headers → quota state
  - Cache quota state (5-min TTL) to avoid probing too often

Quota model (verified 2026-04-19):
  Max plan uses UNIFIED pool — Opus + Sonnet share quota.
  Haiku has separate pool (confirmed via live probe).
  5-hour window + 7-day window, both monitored.

Headers (from live response):
  anthropic-ratelimit-unified-5h-status: allowed|rate_limited
  anthropic-ratelimit-unified-5h-reset: <unix-ts>
  anthropic-ratelimit-unified-5h-utilization: 0.0-1.0
  anthropic-ratelimit-unified-7d-status
  anthropic-ratelimit-unified-7d-reset
  anthropic-ratelimit-unified-7d-utilization
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

KEYCHAIN_SERVICE = "Claude Code-credentials"
OAUTH_REFRESH_URL = "https://claude.ai/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"

QUOTA_CACHE_PATH = Path.home() / ".surrogate" / "yolo" / "max-quota.json"
QUOTA_CACHE_TTL = 300  # 5 minutes

# --- Model IDs (from live probe 2026-04-19) ---
MODEL_OPUS = "claude-opus-4-20250514"
MODEL_SONNET = "claude-sonnet-4-20250514"
MODEL_HAIKU = "claude-haiku-4-5-20251001"


@dataclass
class QuotaState:
    """Rate-limit state parsed from response headers."""
    model: str
    status: str = "unknown"               # allowed | rate_limited | unknown
    reset_at: int = 0                     # unix timestamp when window resets
    utilization_5h: float = 0.0
    utilization_7d: float = 0.0
    last_checked: float = 0.0             # unix seconds
    last_error: str = ""

    @property
    def available(self) -> bool:
        return self.status == "allowed"

    @property
    def seconds_until_reset(self) -> int:
        return max(0, int(self.reset_at - time.time()))


@dataclass
class MaxResponse:
    """Successful response from Max plan."""
    content: str
    model_requested: str
    model_served: str
    input_tokens: int
    output_tokens: int
    quota: QuotaState = field(default_factory=lambda: QuotaState(model=""))


class MaxUnavailable(Exception):
    """Raised when Max plan cannot serve the request (429 or auth)."""
    def __init__(self, model: str, reset_at: int = 0, msg: str = ""):
        self.model = model
        self.reset_at = reset_at
        self.msg = msg
        super().__init__(f"Max {model} unavailable: {msg} (reset in {max(0, reset_at - int(time.time()))}s)")


class MaxAuthError(Exception):
    """Raised when OAuth token refresh fails permanently — needs relogin."""


# ----------------------------------------------------------------------
# Keychain I/O
# ----------------------------------------------------------------------
def read_token() -> dict:
    """Read full credential blob from keychain."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return json.loads(raw)
    except subprocess.CalledProcessError:
        raise MaxAuthError(f"Keychain entry '{KEYCHAIN_SERVICE}' not found — run `claude` to login")
    except json.JSONDecodeError as e:
        raise MaxAuthError(f"Invalid JSON in keychain: {e}")


def write_token(cred: dict) -> None:
    """Atomically replace keychain entry."""
    body = json.dumps(cred)
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["security", "add-generic-password",
         "-s", KEYCHAIN_SERVICE,
         "-a", os.environ.get("USER", "Ashira"),
         "-w", body,
         "-U"],
        check=True,
        stderr=subprocess.DEVNULL,
    )


# ----------------------------------------------------------------------
# OAuth refresh
# ----------------------------------------------------------------------
def refresh_if_needed(cred: dict, buffer_seconds: int = 120) -> dict:
    """Refresh access token if expiring in <buffer_seconds. Writes back to keychain."""
    oa = cred["claudeAiOauth"]
    expires_at = oa["expiresAt"] / 1000
    if time.time() + buffer_seconds < expires_at:
        return cred  # still fresh

    # Refresh
    req = urllib.request.Request(
        OAUTH_REFRESH_URL,
        data=json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": oa["refreshToken"],
            "client_id": OAUTH_CLIENT_ID,
        }).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            new = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise MaxAuthError(
            f"OAuth refresh failed ({e.code}): {e.read().decode()[:200]}. "
            "Run `claude` in a new terminal to re-login."
        )

    oa["accessToken"] = new["access_token"]
    oa["refreshToken"] = new["refresh_token"]
    oa["expiresAt"] = int((time.time() + new["expires_in"]) * 1000)
    write_token(cred)
    return cred


# ----------------------------------------------------------------------
# Quota cache
# ----------------------------------------------------------------------
def load_quota_cache() -> dict[str, QuotaState]:
    """Load cached quota state (per model)."""
    if not QUOTA_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(QUOTA_CACHE_PATH.read_text())
        return {k: QuotaState(**v) for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError):
        return {}


def save_quota_cache(cache: dict[str, QuotaState]) -> None:
    QUOTA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v.__dict__ for k, v in cache.items()}
    QUOTA_CACHE_PATH.write_text(json.dumps(data, indent=2))


def parse_quota_headers(model: str, headers: dict[str, str]) -> QuotaState:
    """Parse anthropic-ratelimit-* headers into QuotaState."""
    h = {k.lower(): v for k, v in headers.items()}

    def fget(key: str, default: float = 0.0) -> float:
        try:
            return float(h.get(key, default))
        except (ValueError, TypeError):
            return default

    def iget(key: str, default: int = 0) -> int:
        try:
            return int(float(h.get(key, default)))
        except (ValueError, TypeError):
            return default

    status = h.get("anthropic-ratelimit-unified-5h-status", "unknown")
    reset_5h = iget("anthropic-ratelimit-unified-5h-reset")
    reset_7d = iget("anthropic-ratelimit-unified-7d-reset")

    return QuotaState(
        model=model,
        status=status,
        reset_at=max(reset_5h, reset_7d) if reset_5h and reset_7d else reset_5h or reset_7d,
        utilization_5h=fget("anthropic-ratelimit-unified-5h-utilization"),
        utilization_7d=fget("anthropic-ratelimit-unified-7d-utilization"),
        last_checked=time.time(),
    )


# ----------------------------------------------------------------------
# Call Anthropic via Max OAuth
# ----------------------------------------------------------------------
def call_max(
    model: str,
    messages: list[dict],
    max_tokens: int = 4096,
    system: Optional[str] = None,
    timeout: int = 180,
) -> MaxResponse:
    """Make a Max-plan OAuth call. Raises MaxUnavailable on 429."""
    cred = refresh_if_needed(read_token())
    token = cred["claudeAiOauth"]["accessToken"]

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system

    req = urllib.request.Request(
        ANTHROPIC_API,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_BETA,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            quota = parse_quota_headers(model, dict(r.getheaders()))
            _update_cache(quota)
            return MaxResponse(
                content=data["content"][0]["text"],
                model_requested=model,
                model_served=data.get("model", model),
                input_tokens=data["usage"]["input_tokens"],
                output_tokens=data["usage"]["output_tokens"],
                quota=quota,
            )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        headers = dict(e.headers)
        quota = parse_quota_headers(model, headers)
        # Override: 429 always means rate_limited regardless of header contents
        quota.status = "rate_limited" if e.code == 429 else "error"
        quota.last_error = f"HTTP {e.code}: {err_body[:200]}"
        # If 429 but no reset header, set a safe cooldown (5 min) so pick_max_model skips
        if e.code == 429 and quota.reset_at <= time.time():
            quota.reset_at = int(time.time() + 300)
        _update_cache(quota)
        if e.code == 429:
            raise MaxUnavailable(model, quota.reset_at, err_body)
        if e.code == 401:
            raise MaxAuthError(f"Max auth failed ({e.code}) — relogin needed")
        raise MaxUnavailable(model, 0, f"HTTP {e.code}: {err_body[:200]}")


def _update_cache(quota: QuotaState) -> None:
    cache = load_quota_cache()
    cache[quota.model] = quota
    save_quota_cache(cache)


# ----------------------------------------------------------------------
# Tier selection
# ----------------------------------------------------------------------
MAX_TIER_ORDER = [MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU]


def pick_max_model(prefer: str = MODEL_OPUS) -> Optional[str]:
    """Pick best available Max-plan model.

    Strategy:
      1. If cache status=allowed AND fresh (< TTL) → use it immediately
      2. If cache stale (> TTL) → eligible to re-probe (real probe will confirm)
      3. If cache rate_limited:
           - If reset_at > 0 AND reset_at still in future → NOT eligible (honor cooldown)
           - Only eligible when reset_at passed + cache went stale
      4. Walk Opus → Sonnet → Haiku; use first eligible

    Returns model name or None if all rate-limited within cooldown.
    """
    cache = load_quota_cache()
    now = time.time()

    def eligible(model: str) -> bool:
        q = cache.get(model)
        if not q:
            return True  # unknown → worth one probe
        # Fresh + allowed
        if q.status == "allowed" and now - q.last_checked <= QUOTA_CACHE_TTL:
            return True
        # Rate-limited + still within cooldown window → skip
        if q.status == "rate_limited" and q.reset_at > now:
            return False
        # Stale (either status) + no active cooldown → re-probe OK
        if now - q.last_checked > QUOTA_CACHE_TTL:
            return True
        # Rate-limited but reset_at is 0 or in past → try again cautiously
        if q.status == "rate_limited" and q.reset_at <= now:
            return now - q.last_checked > 30  # wait 30s between retries
        return False

    order = [prefer] + [m for m in MAX_TIER_ORDER if m != prefer]
    for model in order:
        if eligible(model):
            return model
    return None


def probe_and_refresh_cache() -> dict[str, QuotaState]:
    """Send minimal probes to each tier to refresh cache. Called every 5 min."""
    out: dict[str, QuotaState] = {}
    for model in MAX_TIER_ORDER:
        try:
            resp = call_max(model, [{"role": "user", "content": "."}], max_tokens=5)
            out[model] = resp.quota
        except MaxUnavailable as e:
            # already cached in _update_cache
            cache = load_quota_cache()
            out[model] = cache.get(model, QuotaState(model=model, status="rate_limited",
                                                    reset_at=e.reset_at))
        except MaxAuthError:
            raise
    return out


if __name__ == "__main__":
    # CLI self-test
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        for model, q in probe_and_refresh_cache().items():
            print(f"{model}: {q.status}  util5h={q.utilization_5h:.2f}  "
                  f"reset_in={q.seconds_until_reset}s")
    elif len(sys.argv) > 1 and sys.argv[1] == "pick":
        print(pick_max_model() or "NONE_AVAILABLE")
    else:
        # quick call
        m = pick_max_model() or MODEL_HAIKU
        r = call_max(m, [{"role": "user", "content": sys.argv[1] if len(sys.argv) > 1 else "hi"}], max_tokens=50)
        print(f"[{r.model_served}] {r.content[:200]}")
