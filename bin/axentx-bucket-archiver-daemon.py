#!/usr/bin/env python3
"""axentx bucket archiver — stream large jsonls → Cloudflare R2 (free 10GB).

Watches local jsonl hotspots. Anything >ROTATE_MB:
  1. compress with gzip
  2. upload to R2 bucket axentx-archive at path archive/YYYY-MM-DD/<host>/<name>.jsonl.gz
  3. delete local file (kept-tail already retained by disk-janitor)

R2 free tier: 10GB storage + 10M Class-A ops/mo + 1M Class-B ops/mo.
We use ~5MB/file × ~100 files/day = 500MB/day fits comfortably.

Auth: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID from env file.
Uses S3-compatible API at https://<account>.r2.cloudflarestorage.com.
No boto3 dependency — minimal urllib + sigv4 implementation.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import hmac
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from socket import gethostname

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

POLL_SEC = int(os.environ.get("ARCHIVER_POLL_SEC", "300"))
ROTATE_MB = int(os.environ.get("ARCHIVER_ROTATE_MB", "100"))
HOSTNAME = gethostname()

# R2 credentials. Use S3-compat keys derived from CF API token via the
# Cloudflare dashboard's "Manage R2 API Tokens" page. For simplicity here
# we expect R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY in env (per-bucket).
CF_ACCT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "axentx-archive")
R2_ENDPOINT = (f"https://{CF_ACCT}.r2.cloudflarestorage.com"
               if CF_ACCT else "")

HOTSPOTS = [
    Path("/opt/surrogate-1-harvest/state"),
    Path("/home/ubuntu/axentx/surrogate/data/training-jsonl"),
]

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ── SigV4 (minimal, R2-compatible, no boto) ───────────────────────────────
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str):
    k_date = _sign(("AWS4" + secret).encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def r2_put(key: str, body: bytes,
           content_type: str = "application/octet-stream") -> bool:
    """PUT object to R2 via S3-compat SigV4."""
    if not (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET):
        return False
    region = "auto"
    service = "s3"
    method = "PUT"
    host = R2_ENDPOINT.replace("https://", "")
    canonical_uri = f"/{R2_BUCKET}/{key}"
    payload_hash = hashlib.sha256(body).hexdigest()

    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"{method}\n{canonical_uri}\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    signing_key = _signing_key(R2_SECRET, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={R2_ACCESS_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = urllib.request.Request(
        f"{R2_ENDPOINT}{canonical_uri}", data=body, method=method,
        headers={
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": auth,
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log("archiver", f"  PUT {key}: HTTP {e.code} {e.read()[:160]}")
        return False
    except Exception as e:
        log("archiver",
            f"  PUT {key}: {type(e).__name__}: {str(e)[:120]}")
        return False


def archive_file(path: Path) -> int:
    """Compress + upload + delete. Returns bytes saved locally."""
    try:
        sz = path.stat().st_size
    except Exception:
        return 0
    raw = path.read_bytes()
    body = gzip.compress(raw, compresslevel=6)
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    key = f"archive/{today}/{HOSTNAME}/{path.name}.gz"
    if not r2_put(key, body, "application/gzip"):
        log("archiver", f"  ✗ upload failed: {path}")
        return 0
    # Verify upload by HEAD before deleting (simplified — trust 200 for now)
    log("archiver",
        f"  ✓ archived {path.name}: {sz//1_000_000}MB → r2:{key} "
        f"({len(body)//1_000_000}MB gzipped)")
    path.unlink(missing_ok=True)
    return sz


def main() -> int:
    if not (R2_ACCESS_KEY and R2_SECRET and CF_ACCT):
        log("archiver",
            "⚠ R2 creds not set (R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/"
            "CLOUDFLARE_ACCOUNT_ID) — daemon idle until configured")
    log("archiver",
        f"start — poll {POLL_SEC}s, rotate when >{ROTATE_MB}MB → "
        f"r2:{R2_BUCKET}")
    while not _stop:
        cycle_start = time.time()
        archived_total = 0
        for hot in HOTSPOTS:
            if _stop:
                break
            if not hot.exists():
                continue
            for f in hot.rglob("*.jsonl"):
                if _stop:
                    break
                if f.is_symlink():
                    continue
                try:
                    if f.stat().st_size > ROTATE_MB * 1_000_000:
                        archived_total += archive_file(f)
                except Exception:
                    continue
        if archived_total:
            log("archiver",
                f"cycle archived {archived_total//1_000_000}MB total")
        nap = max(0, POLL_SEC - (time.time() - cycle_start))
        for _ in range(int(nap)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
