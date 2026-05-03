# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Pre-lists target folder once via HF API (cached), saves `manifest-{DATE}.json`
- Downloads only assigned shard’s files via **CDN bypass** (`resolve/main/...` URLs, no auth header)
- Projects heterogeneous schemas → `{prompt, response}` at parse time (avoids pyarrow `CastError`)
- Dedups via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)
- Includes retry/backoff for 429s and idle-stop handling for Lightning reuse

---

## Changes

### 1) New `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
    HF_TOKEN=hf_xxx python bin/dataset-enrich.py

Behavior:
- Single repo-tree list per DATE -> manifest-{DATE}.json (cached locally)
- Each shard downloads only its slice via HF CDN (no /api/ auth)
- Projects heterogeneous schemas -> {prompt, response}
- Dedup via lib/dedup.py (central md5 store)
- Outputs batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
- Exits 0 on success, non-zero on fatal error
"""

import os
import sys
import json
import time
import hashlib
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# ----------------------------
# Configuration
# ----------------------------
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUTPUT_DIR = Path("batches/public-merged") / DATE
MANIFEST_PATH = Path(f"manifest-{DATE}.json")
RETRY_WAIT = int(os.getenv("RETRY_WAIT", "360"))  # on 429
TIMEOUT = int(os.getenv("TIMEOUT", "120"))
IDLE_STOP = int(os.getenv("IDLE_STOP", "300"))  # seconds without progress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

# ----------------------------
# Graceful shutdown
# ----------------------------
interrupted = False

def _signal_handler(signum, frame):
    global interrupted
    interrupted = True
    log.warning("Received signal %s, stopping after current file", signum)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ----------------------------
# Dedup store (central)
# ----------------------------
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa

dedup = DedupStore()

# ----------------------------
# Helpers
# ----------------------------
def slug_hash(s: str) -> int:
    """Deterministic 0..SHARD_TOTAL-1 bucket for a file slug."""
    return int(hashlib.sha256(s.encode()).hexdigest(), 16) % SHARD_TOTAL

def hf_api_with_retry():
    return HfApi(token=HF_TOKEN)

def get_manifest() -> List[str]:
    """Return list of file paths for DATE folder (cached)."""
    if MANIFEST_PATH.exists():
        log.info("Using cached manifest: %s", MANIFEST_PATH)
        return json.loads(MANIFEST_PATH.read_text())

    api = hf_api_with_retry()
    log.info("Listing repo tree for %s/%s", HF_REPO, DATE)
    try:
        tree = list_repo_tree(
            repo_id=HF_REPO,
            path=DATE,
            repo_type="dataset",
            token=HF_TOKEN,
        )
    except Exception as exc:
        log.exception("Failed to list repo tree")
        raise

    # Keep only parquet/jsonl files under DATE/
    files = [
        f.rfilename
        for f in tree
        if f.rfilename.endswith((".parquet", ".jsonl"))
    ]
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    log.info("Saved manifest with %d files", len(files))
    return files

def cdn_download(url: str, dest: Path) -> Path:
    """Download via HF CDN (no Authorization header -> bypass /api/ limits)."""
    for attempt in range(5):
        if interrupted:
            raise KeyboardInterrupt
        try:
            with requests.get(url, timeout=TIMEOUT, stream=True) as r:
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if interrupted:
                            raise KeyboardInterrupt
                        f.write(chunk)
            return dest
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = RETRY_WAIT * (2 ** attempt)
                log.warning("429 CDN rate-limited, waiting %ss", wait)
                time.sleep(wait)
                continue
            raise
        except Exception:
            log.exception("Download failed (attempt %s)", attempt + 1)
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Exhausted retries")

def project_to_pair(obj: Dict[str, Any], filename: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports common patterns seen in surrogate-1 sources.
    """
    # Direct fields
    if "prompt" in obj and "response" in obj:
        return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}

    # Alternate casing
    p = obj.get("prompt") or obj.get("Prompt") or obj.get("PROMPT") or obj.get("input") or obj.get("Input") or obj.get("INPUT") or obj.get("question")
    r = obj.get("response") or obj.get("Response") or obj.get("RESPONSE") or obj.get("output") or obj.get("Output") or obj.get("OUTPUT") or obj.get("answer")
    if p is not None and r is not None:
        return {"prompt": str(p), "response": str(r)}

    # Chat-style list
    messages = obj.get("messages") or obj.get("conversation")
    if isinstance(messages, list) and len(messages) >= 2:
        # last assistant as response, preceding as prompt
        assistant_msgs = [m for m in messages if m.get("role") in ("assistant", "model", "bot")]
        user_msgs = [m for m in messages if m.get("role") in ("user", "human")]
        if assistant_msgs and user_msgs:
            return {
                "prompt": " ".join(str(m.get("content", "")) for m in user_msgs),
                "response": str(assistant_msgs[-1].get("content", "")),
            }

    # Fallback: any two text fields
    text_keys = [k for k in obj if isinstance(obj[k], str) and len(str(obj[k])) > 20]
    if len(text_keys) >= 2:
        return {"prompt": str(obj[text_keys[0]]), "response": str(obj[text_keys[1]])}

    log.debug("Could not project pair from %s", filename)
    return None

# ----------------
