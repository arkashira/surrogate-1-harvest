"""Shared HTTP retry library for all cloud bridges.
Handles: exponential backoff + jitter + Retry-After + circuit breaker.
Import at top of any bridge: exec(open(...).read())

Exports: request_with_retry(url, data, headers, max_retries=4, base_delay=2.0)
"""
import json as _json
import os as _os
import random as _random
import time as _time
import urllib.request as _urlreq
import urllib.error as _urlerr

# Circuit breaker state — persisted in /tmp so all bridge invocations share
_CB_DIR = "/tmp/bridge-circuits"
_os.makedirs(_CB_DIR, exist_ok=True)


def _cb_state_path(host):
    return f"{_CB_DIR}/{host.replace('/', '_')}.json"


def _circuit_open(host):
    p = _cb_state_path(host)
    try:
        with open(p) as f:
            s = _json.load(f)
        # Circuit closed after timeout
        if _time.time() > s.get("open_until", 0):
            return False, 0
        return True, int(s["open_until"] - _time.time())
    except Exception:
        return False, 0


def _record_failure(host, open_seconds=60):
    """Called on 429 or 5xx — track consecutive failures."""
    p = _cb_state_path(host)
    try:
        with open(p) as f:
            s = _json.load(f)
    except Exception:
        s = {"consec_fails": 0, "open_until": 0}
    s["consec_fails"] = s.get("consec_fails", 0) + 1
    # Open circuit after 3 consecutive failures
    if s["consec_fails"] >= 3:
        s["open_until"] = _time.time() + open_seconds
    with open(p, "w") as f:
        _json.dump(s, f)


def _record_success(host):
    """Called on 2xx — reset failure counter."""
    p = _cb_state_path(host)
    try:
        with open(p, "w") as f:
            _json.dump({"consec_fails": 0, "open_until": 0}, f)
    except Exception:
        pass


def _parse_retry_after(headers, default_delay):
    """Honor Retry-After header (seconds) or x-ratelimit-reset-after."""
    for h in ("Retry-After", "retry-after", "x-ratelimit-reset-after", "x-ratelimit-reset"):
        val = headers.get(h)
        if val:
            try:
                n = int(val)
                # x-ratelimit-reset may be absolute epoch — convert to delta
                if n > 10_000_000_000:  # way in future = epoch ms
                    n = n // 1000 - int(_time.time())
                elif n > 1_000_000_000:  # epoch seconds
                    n = n - int(_time.time())
                return max(1, min(n, 300))  # clamp 1..300s
            except (ValueError, TypeError):
                pass
    return default_delay


def request_with_retry(url, data, headers, timeout=120, max_retries=4, base_delay=2.0, open_seconds=60):
    """Make HTTP request with exp-backoff retry + circuit breaker.

    Args:
      open_seconds: how long to open circuit after 3 consecutive failures.
        Default 60s. Callers with strict per-minute rate limits (Cloudflare,
        SambaNova) should use 120-180s so we don't hammer during cooldown.

    Returns: parsed JSON response.
    Raises: Exception if circuit open or max retries exhausted.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc

    # Circuit breaker check
    is_open, remaining = _circuit_open(host)
    if is_open:
        raise Exception(f"circuit-open for {host} ({remaining}s remaining)")

    last_err = None
    for attempt in range(max_retries):
        try:
            req = _urlreq.Request(url, data=data, headers=headers)
            with _urlreq.urlopen(req, timeout=timeout) as r:
                result = _json.load(r)
                _record_success(host)
                return result
        except _urlerr.HTTPError as e:
            last_err = e
            if e.code == 429:
                # Rate-limited — honor Retry-After
                base = base_delay * (2 ** attempt)
                delay = _parse_retry_after(e.headers, base)
                delay *= (1 + _random.uniform(-0.2, 0.2))  # jitter ±20%
                if attempt < max_retries - 1:
                    _time.sleep(min(delay, 60))
                    continue
                _record_failure(host, open_seconds=open_seconds)
                raise Exception(f"HTTP 429 after {max_retries} retries (last Retry-After: {delay:.0f}s)")
            elif 500 <= e.code < 600:
                # Server error — exp backoff with jitter
                delay = base_delay * (2 ** attempt) * (1 + _random.uniform(-0.2, 0.2))
                if attempt < max_retries - 1:
                    _time.sleep(min(delay, 30))
                    continue
                _record_failure(host, open_seconds=open_seconds)
                raise Exception(f"HTTP {e.code} after {max_retries} retries")
            else:
                # 4xx other than 429 — not retryable (client error)
                _record_failure(host, open_seconds=open_seconds)
                raise
        except (_urlerr.URLError, _os.error) as e:
            last_err = e
            # Network error — retry with backoff
            delay = base_delay * (2 ** attempt) * (1 + _random.uniform(-0.2, 0.2))
            if attempt < max_retries - 1:
                _time.sleep(min(delay, 30))
                continue
            _record_failure(host, open_seconds=open_seconds)
            raise

    raise Exception(f"max retries ({max_retries}) exhausted: {last_err}")
