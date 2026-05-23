#!/usr/bin/env python3
"""axentx archiver — stream large jsonls → HuggingFace dataset (free).

Pivoted 2026-05-03 from Cloudflare R2 to HF Hub because:
  - R2 access keys must be created via Cloudflare dashboard (the
    'Manage R2 API Tokens' page); the existing CF_API_TOKEN does not
    have r2_admin scope, so we cannot bootstrap R2 creds without
    interactive web action by the user.
  - HF token (axentx admin) already in env. axentx/surrogate-1-archive
    private dataset gives unlimited storage for the org. No new auth.
  - Same byte-for-byte compression target (~5-10x via gzip).
  - upload_file is one HTTP call (vs SigV4 sig + PUT), ~10 LOC vs 100.

Watches local jsonl hotspots. Anything >ROTATE_MB:
  1. compress with gzip
  2. upload to axentx/surrogate-1-archive at
     archive/YYYY-MM-DD/<host>/<filename>.gz
  3. delete local file (kept-tail already retained by disk-janitor)

Idempotent. If upload fails (rate limit, network, auth) the local
file stays — janitor will rotate-tail later if disk pressure hits.
"""
from __future__ import annotations

import datetime
import gzip
import os
import signal
import sys
import time
from pathlib import Path
from socket import gethostname

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

POLL_SEC = int(os.environ.get("ARCHIVER_POLL_SEC", "300"))
ROTATE_MB = int(os.environ.get("ARCHIVER_ROTATE_MB", "100"))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_REPO = os.environ.get("ARCHIVER_HF_REPO", "axentx/surrogate-1-archive")
HOSTNAME = gethostname()

HOTSPOTS = [
    Path("/opt/surrogate-1-harvest/state"),
    Path("/home/ubuntu/axentx/surrogate/data/training-jsonl"),
    Path("/opt/surrogate-1-state"),
]

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _ensure_hf() -> "object | None":
    """Lazy-import HfApi so the daemon survives even if huggingface_hub
    isn't installed in the venv yet — log + idle instead of crash."""
    try:
        from huggingface_hub import HfApi, create_repo
    except ImportError:
        log("archiver",
            "⚠ huggingface_hub not installed — pip install in venv first")
        return None
    api = HfApi(token=HF_TOKEN or None)
    # Ensure repo exists (idempotent — exist_ok=True)
    try:
        create_repo(HF_REPO, repo_type="dataset", token=HF_TOKEN or None,
                    exist_ok=True, private=True)
    except Exception as e:
        log("archiver",
            f"  create_repo {HF_REPO}: {type(e).__name__}: {str(e)[:120]}")
    return api


SAFE_MAX_BYTES = int(os.environ.get("ARCHIVER_SAFE_MAX_MB", "1000")) * 1_000_000


def archive_file(api, path: Path) -> int:
    """Compress + upload + delete. Returns bytes saved locally.

    Uses gzip stream-write to a tempfile so we don't load the whole file
    into RAM — daemon RSS stays under MemoryMax even on 1GB jsonls.
    """
    try:
        sz = path.stat().st_size
    except Exception:
        return 0
    if sz > SAFE_MAX_BYTES:
        log("archiver",
            f"  ⚠ skip {path.name}: {sz//1_000_000}MB > "
            f"{SAFE_MAX_BYTES//1_000_000}MB safety cap "
            f"(let janitor rotate-tail it)")
        return 0
    # Stream-compress: read in chunks, write gz to tempfile.
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".gz", delete=False)
    try:
        with open(path, "rb") as f_in, gzip.GzipFile(
            fileobj=tmp, mode="wb", compresslevel=6,
        ) as gz:
            while True:
                chunk = f_in.read(4 * 1024 * 1024)  # 4MB chunks
                if not chunk:
                    break
                gz.write(chunk)
        tmp.close()
        body_size = os.path.getsize(tmp.name)
        with open(tmp.name, "rb") as fh:
            body = fh.read()
    except Exception as e:
        log("archiver", f"  compress fail {path.name}: {type(e).__name__}")
        os.unlink(tmp.name)
        return 0
    os.unlink(tmp.name)
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    remote = f"archive/{today}/{HOSTNAME}/{path.name}.gz"
    try:
        api.upload_file(
            path_or_fileobj=body,
            path_in_repo=remote,
            repo_id=HF_REPO,
            repo_type="dataset",
            commit_message=(
                f"archive {path.name} ({sz//1_000_000}MB→"
                f"{len(body)//1_000_000}MB) from {HOSTNAME}"
            ),
        )
    except Exception as e:
        log("archiver",
            f"  ✗ upload {path.name}: {type(e).__name__}: {str(e)[:160]}")
        return 0
    log("archiver",
        f"  ✓ {path.name}: {sz//1_000_000}MB → "
        f"hf:{HF_REPO}/{remote} ({body_size//1_000_000}MB gz)")
    try:
        path.unlink()
    except Exception:
        pass
    # Free body asap
    del body
    return sz


def main() -> int:
    if not HF_TOKEN:
        log("archiver", "⚠ HF_TOKEN not set — daemon idle")
        # Don't exit; if env later becomes available we'll pick it up
    log("archiver",
        f"start — poll {POLL_SEC}s, rotate when >{ROTATE_MB}MB → hf:{HF_REPO}")
    api = _ensure_hf() if HF_TOKEN else None

    while not _stop:
        cycle_start = time.time()
        if api is None and HF_TOKEN:
            # Retry init on each cycle — package may have just installed
            api = _ensure_hf()

        archived_total = 0
        if api is not None:
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
                            archived_total += archive_file(api, f)
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
