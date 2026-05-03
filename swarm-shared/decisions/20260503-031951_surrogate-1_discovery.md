# surrogate-1 / discovery

### Final Implementation Plan  
**Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker (`bin/dataset-enrich.py`) that deterministically shards, avoids HF API rate limits, projects heterogeneous schemas, and reuses existing compute resources.**

---

### Core Design Decisions (resolved)
- **Deterministic sharding**: use `hash(slug) % SHARD_TOTAL` (Candidate 2) — avoids fragile positional slicing and survives file additions/removals.  
- **Single manifest call**: `list_repo_tree(path, recursive=False)` once per date folder (Candidate 2) — minimizes API usage.  
- **CDN-only data fetch**: `https://huggingface.co/datasets/.../resolve/main/...` with **no Authorization header** during download (Candidate 2) — prevents 429s.  
- **Schema projection at parse time**: map heterogeneous inputs to `{prompt, response}` only when needed (Candidate 2) — avoids pyarrow cast errors and keeps stored data minimal.  
- **Deduplication**: reuse `lib/dedup.py` md5 store (both candidates) — central, consistent.  
- **Output format**: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns; attribution via filename pattern (Candidate 2).  
- **Compute reuse**: if a Lightning Studio with matching name is running, reuse it instead of recreating (Candidate 2) — saves quota.  
- **Invocation**: prefer `python bin/dataset-enrich.py` in CI; use `bash` wrappers only when necessary, with proper shebang and `SHELL=/bin/bash` in crontab (Candidate 2).  
- **Env interface**: `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (both candidates).

---

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Deterministic sharding + CDN-only fetches to avoid HF API rate limits.
"""
import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# ── config ────────────────────────────────────────────────────────────────
REPO_ID = "axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
API = HfApi(token=HF_TOKEN)

# ── deterministic shard assignment ────────────────────────────────────────
def shard_for_slug(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

# ── list files for date (single API call) ────────────────────────────────
def list_date_files(date: str) -> List[str]:
    """Return file paths under batches/raw/{date}/ (non-recursive)."""
    prefix = f"batches/raw/{date}/"
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=prefix,
            recursive=False,
            token=HF_TOKEN,
        )
        return [t.path for t in tree if t.type == "file"]
    except Exception as e:
        print(f"[WARN] list_repo_tree failed: {e}", file=sys.stderr)
        return []

# ── CDN bypass download ───────────────────────────────────────────────────
def cdn_download(file_path: str) -> bytes:
    """Download via HF CDN (no auth header)."""
    url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

# ── schema projection helpers ─────────────────────────────────────────────
def extract_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Known schemas:
      - {prompt, response}
      - {input, output}
      - {question, answer}
      - {text} -> split on last newline as prompt/response
    Fallback: first two string fields or JSON-encoded prompt.
    """
    if "prompt" in raw and "response" in raw:
        return {"prompt": str(raw["prompt"]), "response": str(raw["response"])}
    if "input" in raw and "output" in raw:
        return {"prompt": str(raw["input"]), "response": str(raw["output"])}
    if "question" in raw and "answer" in raw:
        return {"prompt": str(raw["question"]), "response": str(raw["answer"])}
    if "text" in raw:
        parts = str(raw["text"]).rsplit("\n", 1)
        if len(parts) == 2:
            return {"prompt": parts[0], "response": parts[1]}
        return {"prompt": "", "response": parts[0]}
    str_items = [(k, str(v)) for k, v in raw.items() if isinstance(v, str)]
    if len(str_items) >= 2:
        return {"prompt": str_items[0][1], "response": str_items[1][1]}
    return {"prompt": json.dumps(raw), "response": ""}

# ── dedup via central store ───────────────────────────────────────────────
def is_duplicate(md5_hex: str) -> bool:
    script = Path(__file__).parent / "lib" / "dedup.py"
    if not script.exists():
        return False
    result = subprocess.run(
        [sys.executable, str(script), "--check", md5_hex],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

def register_md5(md5_hex: str) -> None:
    script = Path(__file__).parent / "lib" / "dedup.py"
    if script.exists():
        subprocess.run(
            [sys.executable, str(script), "--add", md5_hex],
            capture_output=True,
        )

# ── main worker logic ─────────────────────────────────────────────────────
def main() -> None:
    files = list_date_files(DATE)
    if not files:
        print(f"[INFO] No files found for {DATE}", file=sys.stderr)
        return

    assigned = [f for f in files if shard_for_slug(Path(f).stem) == SHARD_ID]
    if not assigned:
        print(f"[INFO] No files assigned to shard {SHARD_ID}", file=sys.stderr)
        return

    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    written = 0
    skipped_dup = 0
    failed_dl = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for file_path in assigned:
            try:
                content = cdn_download(file_path)
            except Exception as e:
                print(f"[ERROR] CDN download failed for {file_path}: {e}", file=sys.stderr)
                failed_dl += 1
                continue

            md5_hex = hashlib.md5(content).hexdigest()
            if is_duplicate(md5_hex):
                skipped_dup += 1
                continue

            # parse and normalize
            try:
                raw = json.loads(content)
            except Exception:
                # try line-delimited JSON
                lines = content.decode("utf-8", errors="replace").strip().splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except Exception:
                        continue
                    pair = extract_pair(raw)
                    fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
                register_md5(md5_hex)
                continue

            if isinstance(raw, list):
                for item in raw:
                    pair = extract_pair(item if isinstance(item, dict) else {"text": item})
                    fout.write(json
