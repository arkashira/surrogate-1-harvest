# surrogate-1 / quality

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass shard worker (manifest-driven).

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python3 bin/dataset-enrich.py

Environment:
  SHARD_ID          int   0..SHARD_TOTAL-1
  SHARD_TOTAL       int   default 16
  DATE              str   YYYY-MM-DD folder on dataset repo
  HF_TOKEN          str   write token for axentx/surrogate-1-training-pairs
  SIBLING_REPOS     int   default 5 (for HF commit-cap spreading)
  MANIFEST_PATH     str   optional path to pre-saved manifest JSON
"""

import os
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, list_repo_tree

# --
# Logging
# --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# --
# Config
# --
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = "axentx/surrogate-1-training-pairs"
SIBLING_REPOS = int(os.getenv("SIBLING_REPOS", "5"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH")

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

api = HfApi(token=HF_TOKEN)

# --
# Dedup store (SQLite-backed)
# --
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # noqa: E402

dedup = DedupStore()

# --
# Helpers
# --
def deterministic_shard(key: str, total: int) -> int:
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest, 16) % total

def pick_sibling_repo(slug: str) -> str:
    """Spread writes across sibling repos to avoid HF 128/hr/repo cap."""
    if SIBLING_REPOS <= 1:
        return DATASET_REPO
    idx = deterministic_shard(slug, SIBLING_REPOS)
    if idx == 0:
        return DATASET_REPO
    return f"{DATASET_REPO}-sibling{idx}"

def build_manifest(date_folder: str) -> List[str]:
    """List top-level files in date folder (non-recursive) via API once."""
    try:
        tree = list_repo_tree(
            repo_id=DATASET_REPO,
            path=date_folder,
            repo_type="dataset",
            token=HF_TOKEN,
        )
    except Exception as exc:
        log.error("Failed to list repo tree: %s", exc)
        raise

    files = [item.rfilename for item in tree if item.type == "file"]
    log.info("Manifest built: %d files in %s", len(files), date_folder)
    return files

def cdn_download(url: str, timeout: int = 30) -> bytes:
    """Download via HF CDN (no Authorization header)."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_file_to_pair(content: bytes, filename: str) -> Dict[str, str]:
    """
    Project heterogeneous file to {prompt, response}.
    Extend per known schema; keep minimal and defensive.
    """
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        log.warning("Non-UTF8 file skipped: %s", filename)
        return {}

    if filename.endswith(".jsonl"):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt and response:
                return {"prompt": str(prompt).strip(), "response": str(response).strip()}
    else:
        if text:
            return {"prompt": text, "response": ""}

    return {}

# --
# Main worker
# --
def run() -> None:
    date_folder = f"{DATE}"
    out_dir = Path("batches/public-merged") / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_file = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    # 1) Manifest
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        log.info("Using pre-saved manifest: %s", MANIFEST_PATH)
        with open(MANIFEST_PATH) as f:
            all_files = json.load(f)
    else:
        all_files = build_manifest(date_folder)

    # 2) Assign shard files
    shard_files = [
        f for f in all_files
        if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID
    ]
    log.info("Shard %d/%d -> %d files", SHARD_ID, SHARD_TOTAL, len(shard_files))

    # 3) Process
    written = 0
    skipped_dup = 0
    failed = 0

    with out_file.open("w", encoding="utf-8") as fout:
        for rel_path in shard_files:
            cdn_url = (
                f"https://huggingface.co/datasets/{DATASET_REPO}"
                f"/resolve/main/{rel_path}"
            )
            try:
                content = cdn_download(cdn_url)
            except Exception as exc:
                log.warning("Download failed %s: %s", rel_path, exc)
                failed += 1
                continue

            pair = parse_file_to_pair(content, rel_path)
            if not pair:
                continue

            digest = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            if dedup.is_duplicate(digest):
                skipped_dup += 1
                continue

            dedup.add(digest)

            # Deterministic sibling repo selection (for commit-cap scaling)
            target_repo = pick_sibling_repo(rel_path)

            record = {
                "prompt": pair["prompt"],
                "response": pair["response"],
                "source": rel_path,
                "date": DATE,
                "shard": SHARD_ID,
                "target_repo": target_repo,
            }
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    log.info(
        "Shard %d complete: written=%d skipped_dup=%d failed=%d -> %s",
        SHARD_ID,
        written,
        skipped_dup,
        failed,
        out_file,
    )

    # Optional: save manifest for reuse
    manifest_out = Path("batches/public-merged") / DATE / f"manifest-{DATE}.json"
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    with manifest_out.open("w") as f:
        json.dump(all_files, f)

if __name__ == "__main__":
    run()
```
