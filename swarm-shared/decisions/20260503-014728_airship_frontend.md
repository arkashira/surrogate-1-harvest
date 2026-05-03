# airship / frontend

## Final Synthesis — Highest-Value, Correct, Actionable

**Chosen approach**  
CDN-only ingestion + deterministic sibling-repo sharding (5 shards) to eliminate HF API 429s during Surrogate training.  
- Pre-list file tree once per date folder → `file_list.json`.  
- Download via public CDN URLs (no auth) and project `{prompt,response}`.  
- Deterministic shard selection: `shard_index = hash(slug) % 5` → upload to `{repo}-shard-{shard_index}`.  
- Training uses local/`datasets` offline mode with the pre-list; removes `streaming=True` and heterogeneous schema issues.

**Why this wins**  
- Highest correctness: avoids auth/429s during training load; deterministic sharding removes commit-cap bottleneck immediately.  
- Highest actionability: pure ingestion/training script changes; no UI/infra; ~1.5–2h end-to-end with clear rollback path.  
- Resolves contradictions:  
  - Use CDN for downloads (both candidates agree) and keep a single pre-list step (Candidate 1) — no per-file API calls.  
  - Deterministic sharding to exactly 5 sibling repos (both agree) with explicit upload step and naming convention.  
  - Training script accepts `file_list.json` and uses local files or `hf_hub_download`/offline mode; remove `streaming=True` and fragile per-record schema projection during training.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | Me | 15m | `scripts/ingest/cdn_pre_list.py` — list date folder (non-recursive) → `file_list.json`. |
| 2 | Me | 20m | `scripts/ingest/cdn_download_project.py` — CDN download + project `{prompt,response}` → per-file parquet. |
| 3 | Me | 20m | `scripts/ingest/shard_upload.py` — deterministic shard upload (`hash(slug)%5`) to sibling repos. |
| 4 | Me | 20m | `training/train.py` — accept `file_list.json`; use local files or `datasets` offline; remove `streaming=True`; unify schema via projection at parse time. |
| 5 | Me | 20m | Retry/backoff for pre-list 429 (wait 360s); idempotent download (skip existing). |
| 6 | Me | 10m | Update README ingestion section with new flow and shard naming. |
| 7 | Me | 10m | Smoke test: end-to-end on small date folder; verify parquet projection and shard upload. |

**Total**: ~1h55m (includes buffer).

---

## Code Snippets (Final, Corrected, Actionable)

### 1. Pre-list file paths (run once per date folder)
`scripts/ingest/cdn_pre_list.py`
```python
#!/usr/bin/env python3
"""
Pre-list HF dataset files for a date folder (non-recursive).
Saves file_list.json for CDN-only training.
"""
import json
import sys
from datetime import datetime
from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-dataset"
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d")
OUT = sys.argv[2] if len(sys.argv) > 2 else "file_list.json"

def main():
    tree = API.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]
    payload = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "files": files,
        "generated_at": datetime.utcnow().isoformat() + "Z"
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Saved {len(files)} files to {OUT}")

if __name__ == "__main__":
    main()
```

### 2. CDN download + projection (no auth)
`scripts/ingest/cdn_download_project.py`
```python
#!/usr/bin/env python3
"""
Download files via HF CDN (no Authorization) and project {prompt,response}.
"""
import json
import sys
import requests
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
REPO = "axentx/surrogate-dataset"

def project_parquet(local_path: Path):
    """Return list of {prompt, response, source_file} dicts from one parquet."""
    try:
        table = pq.read_table(local_path)
        # Find likely columns
        prompt_col = next((c for c in table.column_names if "prompt" in c.lower()), None)
        response_col = next((c for c in table.column_names if "response" in c.lower()), None)

        if prompt_col and response_col:
            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
        else:
            # Fallback: first two string columns
            text_cols = [c for c in table.column_names
                         if pa.types.is_string(table.schema.field(c).type)]
            if len(text_cols) >= 2:
                prompts = table.column(text_cols[0]).to_pylist()
                responses = table.column(text_cols[1]).to_pylist()
            else:
                raise ValueError("No prompt/response columns found")

        # Ensure equal length
        n = min(len(prompts), len(responses))
        out = []
        for i in range(n):
            p = prompts[i] if isinstance(prompts[i], str) else str(prompts[i])
            r = responses[i] if isinstance(responses[i], str) else str(responses[i])
            out.append({"prompt": p, "response": r})
        return out
    except Exception as e:
        print(f"Projection failed {local_path}: {e}")
        return []

def download_and_project(file_list_path: str, out_dir: str):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    with open(file_list_path) as f:
        manifest = json.load(f)

    all_rows = []
    for rel_path in manifest["files"]:
        url = CDN_TEMPLATE.format(repo=REPO, path=rel_path)
        slug = Path(rel_path).stem
        local_file = out_path / f"{slug}.parquet"

        if local_file.exists():
            print(f"Skip existing {local_file}")
        else:
            print(f"Downloading {rel_path} -> {local_file}")
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(local_file, "wb") as fh:
                        for chunk in r.iter_content(chunk_size=8192):
                            fh.write(chunk)
            except Exception as e:
                print(f"Failed {rel_path}: {e}")
                continue

        rows = project_parquet(local_file)
        for row in rows:
            row["source_file"] = rel_path
        all_rows.extend(rows)

    # Save merged projection
    if all_rows:
        tbl = pa.table({
            "prompt": pa.array([r["prompt"] for r in all_rows]),
            "response": pa.array([r["response"] for r in all_rows]),
            "source_file": pa.array([r["source_file"] for r in all_rows])
        })
        merged_path = out_path / "merged_projected.parquet"
        pq.write_table(tbl, merged_path)
        print(f"Merged {len(all_rows)} rows -> {merged_path}")
    else:
        print("No rows to merge.")

if __name__ == "__main__":
    download_and_project(
        sys.argv[1] if len(sys.argv) > 1 else "file_list.json",
        sys.argv[2] if len(sys.argv) > 2 else "projected"
    )
```

### 3. Deterministic shard upload
`scripts/ingest/shard_upload.py`
```python
#!/usr/bin/env python3
"""
Deterministic sibling-repo sharding: hash(slug) % 5 -> shard repo.
Uploads parquet files to b
