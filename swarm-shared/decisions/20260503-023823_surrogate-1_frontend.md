# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree` call from Mac (outside training) → `file-list.json` committed to repo per date
- Worker loads manifest, deterministically assigns 1/16 slice by `hash(slug) % 16`
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, no API rate limit
- Projects heterogeneous schemas → `{prompt, response}` only at parse time
- Dedup via central `lib/dedup.py` md5 store
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Adds `requirements.txt` update (keep `datasets` for schema helpers, add `requests`, `tqdm`)
- Keeps GitHub Actions matrix (16 shards) unchanged

---

## 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (local/test):
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py --manifest file-list.json

GitHub Actions sets:
  SHARD_ID (matrix), SHARD_TOTAL=16, DATE (YYYY-MM-DD), HF_TOKEN
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASET_REPO = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
API_BASE = f"https://huggingface.co/api/datasets/{HF_DATASET_REPO}"

# ---- helpers ----
def slug_for_path(path: str) -> str:
    """Stable slug for dedup and shard assignment."""
    return path.strip("/")

def hash_shard(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total

def parse_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Known schema variants handled here; unknown -> best-effort.
    """
    # Common field names seen across datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "output", "answer", "completion", "result"}

    prompt = None
    response = None

    rk = set(raw.keys())
    for pk in prompt_keys:
        if pk in rk and raw[pk] is not None:
            prompt = str(raw[pk]).strip()
            break
    for rk_ in response_keys:
        if rk_ in rk and raw[rk_] is not None:
            response = str(raw[rk_]).strip()
            break

    # Fallbacks
    if prompt is None:
        # try first string field
        for v in raw.values():
            if isinstance(v, str) and v.strip():
                prompt = v.strip()
                break
    if response is None:
        prompt = json.dumps(raw, ensure_ascii=False)
        response = ""

    return {"prompt": prompt or "", "response": response or ""}

# ---- worker ----
def build_manifest(date_folder: str) -> List[str]:
    """
    One-time Mac-side helper: list top-level tree for a date folder.
    Save to file-list.json and commit to repo.
    """
    token = os.environ.get("HF_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # non-recursive: we expect date_folder/<files...>
    r = requests.get(
        f"{API_BASE}/tree",
        params={"path": date_folder, "recursive": "false"},
        headers=headers,
        timeout=30,
    )
    if r.status_code == requests.codes.too_many_requests:
        print("HF API 429 — wait 360s before retry", file=sys.stderr)
        sys.exit(1)
    r.raise_for_status()
    items = r.json()
    paths = [it["path"] for it in items if it["type"] == "file"]
    return sorted(paths)

def download_via_cdn(path: str) -> bytes:
    """CDN bypass: no Authorization header."""
    url = f"{CDN_BASE}/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def worker(
    manifest_path: Path,
    shard_id: int,
    shard_total: int,
    date: str,
    hf_token: str,
    out_dir: Path,
) -> None:
    manifest = json.loads(manifest_path.read_text())
    assigned = [
        p for p in manifest if hash_shard(slug_for_path(p), shard_total) == shard_id
    ]
    print(f"Shard {shard_id}/{shard_total} → {len(assigned)} files")

    dedup = DedupStore()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    outfile = out_dir / f"shard{shard_id}-{ts}.jsonl"

    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}

    written = 0
    skipped_dup = 0
    with outfile.open("w", encoding="utf-8") as f:
        for path in tqdm(assigned, desc="Ingest"):
            try:
                raw_bytes = download_via_cdn(path)
            except Exception as exc:
                print(f"Download failed {path}: {exc}", file=sys.stderr)
                continue

            # Try parquet first (common), then jsonl fallback
            import io
            try:
                import pyarrow.parquet as pq
                table = pq.read_table(io.BytesIO(raw_bytes))
                rows = table.to_pylist()
            except Exception:
                # try jsonl
                try:
                    rows = [json.loads(l) for l in raw_bytes.decode().strip().splitlines() if l.strip()]
                except Exception:
                    print(f"Cannot decode {path}", file=sys.stderr)
                    continue

            for row in rows:
                pair = parse_pair(row)
                if not pair["prompt"] or not pair["response"]:
                    continue
                md5 = dedup.md5_for_pair(pair["prompt"], pair["response"])
                if dedup.exists(md5):
                    skipped_dup += 1
                    continue
                dedup.add(md5)
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

    print(f"Written {written} pairs, skipped {skipped_dup} duplicates -> {outfile}")

def main() -> None:
    shard_id = int(os.environ.get("SHARD_ID", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    date = os.environ.get("DATE")
    hf_token = os.environ.get("HF_TOKEN", "")

    if not date:
        print("DATE env required (YYYY-MM-DD)", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path("file-list.json")
    if not manifest_path.exists():
        print(f"{manifest_path} not found. Generate via build_manifest(date) and commit.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path("batches") / "public-merged" / date
    worker(manifest_path, shard_id, shard_total, date, hf_token, out_dir)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/dataset-enrich.py
```

---

## 2. Keep `lib/dedup.py` unchanged

Assumes existing `lib/dedup.py`
