# vanguard / discovery

# 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- No CDN-only data path: authenticated API calls are used during training when public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) could bypass rate limits entirely.
- Schema pollution: raw files with attribution/metadata columns written directly into `enriched/` instead of projecting to `{prompt, response}` only, risking downstream cast errors and larger parquet files.
- No reuse guard for Lightning Studio: training scripts likely recreate or fail to detect running studios, wasting quota and hitting idle-stop deaths.

# 2. Proposed change

Create `/opt/axentx/vanguard/discovery/persist_filelist.py` (single script, <150 LOC) that:
- Accepts `repo` and `dateFolder` (e.g. `2026-04-29`) on CLI.
- Calls `list_repo_tree(path=dateFolder, recursive=False)` **once** from the Mac orchestrator (after rate-limit window clears).
- Persists `{repo}__{dateFolder}.json` to `/opt/axentx/vanguard/manifests/` containing only filenames (no dirs).
- Emits a companion `train_cdn_only.py` stub that reads the manifest and downloads via CDN URLs with `requests`/`aiohttp` and projects to `{prompt, response}` before yielding examples.
- Adds a small `project_to_pair()` helper that keeps only `prompt`/`response` fields and drops others (attribution → filename pattern).

Scope: new files only; no changes to existing training code until verified.

# 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/manifests /opt/axentx/vanguard/discovery
```

```python
# /opt/axentx/vanguard/discovery/persist_filelist.py
#!/usr/bin/env python3
"""
Usage:
  python persist_filelist.py <repo> <dateFolder>

Produces:
  manifests/{repo}__{dateFolder}.json  -> list of filenames in dateFolder
"""
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub required. pip install huggingface_hub")
    sys.exit(1)

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def persist_filelist(repo: str, date_folder: str) -> Path:
    api = HfApi()
    # Non-recursive to minimize pagination and API calls
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    out_path = MANIFEST_DIR / f"{repo.replace('/', '__')}__{date_folder}.json"
    out_path.write_text(json.dumps(files, indent=2))
    print(f"Persisted {len(files)} files -> {out_path}")
    return out_path

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python persist_filelist.py <repo> <dateFolder>")
        sys.exit(1)
    persist_filelist(sys.argv[1], sys.argv[2])
```

```python
# /opt/axentx/vanguard/discovery/train_cdn_only.py
#!/usr/bin/env python3
"""
CDN-only data loader for surrogate-1 training.
- Reads manifest produced by persist_filelist.py
- Downloads via public CDN (no auth/API calls)
- Projects each file to {prompt, response} only
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterator, Any

import requests

HF_DATASETS_CDN = "https://huggingface.co/datasets"

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests"

def load_manifest(repo: str, date_folder: str) -> list[str]:
    path = MANIFEST_DIR / f"{repo.replace('/', '__')}__{date_folder}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text())

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Keep only prompt/response. Attribution moved to filename pattern.
    Accepts common key variants.
    """
    prompt = (
        raw.get("prompt")
        or raw.get("instruction")
        or raw.get("input")
        or raw.get("question")
        or ""
    )
    response = (
        raw.get("response")
        or raw.get("output")
        or raw.get("answer")
        or raw.get("completion")
        or ""
    )
    # Ensure strings
    return {"prompt": str(prompt), "response": str(response)}

def cdn_url(repo: str, file_path: str) -> str:
    return f"{HF_DATASETS_CDN}/{repo}/resolve/main/{file_path}"

def stream_cdn_examples(repo: str, date_folder: str, files: list[str]) -> Iterator[Dict[str, str]]:
    for fn in files:
        url = cdn_url(repo, f"{date_folder}/{fn}")
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except Exception as exc:
            print(f"WARN: failed {url}: {exc}", file=sys.stderr)
            continue

        # Lightweight heuristic: if JSONL, parse line-by-line; if JSON, parse once.
        # For surrogate-1, assume JSONL or JSON with list of records.
        content = r.content
        if fn.endswith(".jsonl"):
            for line in content.decode("utf-8").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    yield project_to_pair(rec)
                except Exception:
                    continue
        else:
            try:
                data = json.loads(content)
            except Exception:
                print(f"WARN: non-JSON {url}", file=sys.stderr)
                continue

            if isinstance(data, list):
                for rec in data:
                    if isinstance(rec, dict):
                        yield project_to_pair(rec)
            elif isinstance(data, dict):
                yield project_to_pair(data)

def build_dataset(repo: str, date_folder: str) -> list[Dict[str, str]]:
    files = load_manifest(repo, date_folder)
    return list(stream_cdn_examples(repo, date_folder, files))

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python train_cdn_only.py <repo> <dateFolder>")
        sys.exit(1)
    repo, date_folder = sys.argv[1], sys.argv[2]
    examples = build_dataset(repo, date_folder)
    print(f"Built {len(examples)} prompt/response examples (CDN-only).")
    # Quick sanity save
    out_jsonl = Path("sample_examples.jsonl")
    with out_jsonl.open("w") as f:
        for ex in examples[:10]:
            f.write(json.dumps(ex) + "\n")
    print(f"Sample written to {out_jsonl.resolve()}")
```

```bash
# Make helpers executable
chmod +x /opt/axentx/vanguard/discovery/persist_filelist.py
chmod +x /opt/axentx/vanguard/discovery/train_cdn_only.py
```

# 4. Verification

1. **Manifest creation** (run once per dateFolder after HF API window clears):
   ```bash
   cd /opt/axentx/vanguard
   python discovery/persist_filelist.py datasets/your-repo 2026-04-29
   ```
   - Expect: `manifests/datasets__your-repo__2026-04-29.json` with non-empty list of filenames.

2. **CDN-only load test** (no HF API calls during this step):
   ```bash
   python discovery/train_cdn_only.py datasets/
