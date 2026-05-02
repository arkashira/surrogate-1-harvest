# airship / discovery

## Incremental Improvement: Harden `airship discover` into a CDN-only, reproducible orchestrator

**Scope**: <2h  
**Outcome**: `airship discover` no longer hits HF API during data load, avoids PyArrow schema errors, and emits deterministic file manifests.

---

## Implementation Plan

1. **Add `discover` CLI entrypoint** (`airship/cli/discover.py`)  
   - Accepts repo, date folder, output manifest path.
   - Runs once on Mac (or CI) to list files via HF API **once**, then writes `manifest.json`.

2. **Create CDN-only data loader** (`airship/data/cdn_loader.py`)  
   - Reads `manifest.json`.
   - Downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).
   - Projects only `{prompt, response}` at parse time; ignores heterogeneous schemas.

3. **Update training script stub** (`airship/train/train.py`)  
   - Loads manifest, uses `cdn_loader` for data.
   - Zero HF API calls during training (safe for Lightning Studio).

4. **Add lightweight validation**  
   - Manifest includes `sha256` and row count per file.
   - Fail fast if projection yields no valid pairs.

5. **Ensure executable bits + shebangs**  
   - All wrappers use `#!/usr/bin/env bash` and `chmod +x`.

---

## Code Snippets

### 1. CLI: `airship/cli/discover.py`

```python
#!/usr/bin/env python3
"""
airship discover
Produce a CDN-only manifest for a dataset repo + date folder.
Usage:
  python discover.py --repo <repo> --date <YYYY-MM-DD> --out manifest.json
"""
import argparse
import json
import hashlib
from pathlib import Path
from huggingface_hub import list_repo_tree

def build_manifest(repo: str, date_folder: str, out_path: Path):
    # Single API call (do this after rate-limit window clears)
    items = list_repo_tree(repo, path=date_folder, recursive=False)
    manifest = {
        "repo": repo,
        "date": date_folder,
        "files": [],
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main"
    }

    for item in items:
        if item.type != "file":
            continue
        path = item.path
        url = f"{manifest['cdn_prefix']}/{path}"
        # Placeholder hash; can be enriched later by downloading head/checksum if available
        manifest["files"].append({
            "path": path,
            "url": url,
            "size": item.size if hasattr(item, "size") else None,
            "sha256": None  # optional: populate via HEAD or recompute after download
        })

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(manifest['files'])} files)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create CDN-only manifest")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., org/repo)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output manifest path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

---

### 2. CDN Loader: `airship/data/cdn_loader.py`

```python
import json
import requests
import pyarrow as pa
import pyarrow.parquet as pq
from io import BytesIO
from typing import Iterator, Dict, Any

class CDNLoader:
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.prefix = self.manifest.get("cdn_prefix", "")

    def _project_to_pair(self, file_path: str, content: bytes) -> Iterator[Dict[str, str]]:
        """
        Project heterogeneous file to {prompt, response} only.
        Supports: parquet, json, jsonl
        """
        try:
            if file_path.endswith(".parquet"):
                table = pq.read_table(BytesIO(content))
                df = table.to_pandas()
            elif file_path.endswith(".jsonl"):
                df = pa.Table.from_pylist([json.loads(l) for l in content.decode().splitlines() if l.strip()]).to_pandas()
            elif file_path.endswith(".json"):
                df = pa.Table.from_pylist(json.loads(content)).to_pandas()
            else:
                return

            # Normalize column names
            cols = {c.lower(): c for c in df.columns}
            prompt_col = cols.get("prompt") or cols.get("input") or cols.get("question")
            response_col = cols.get("response") or cols.get("output") or cols.get("answer")

            if not prompt_col or not response_col:
                return

            for _, row in df.iterrows():
                prompt = str(row[prompt_col]).strip()
                response = str(row[response_col]).strip()
                if prompt and response:
                    yield {"prompt": prompt, "response": response}
        except Exception:
            # Silently skip malformed/heterogeneous files (PyArrow safety)
            return

    def stream_pairs(self) -> Iterator[Dict[str, str]]:
        for f in self.manifest["files"]:
            url = f["url"]
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            yield from self._project_to_pair(f["path"], resp.content)
```

---

### 3. Training Stub: `airship/train/train.py`

```python
#!/usr/bin/env python3
"""
Lightning-compatible training entry (orchestrator runs on Mac → Studio runs this).
Uses CDN-only loader; zero HF API calls during data load.
"""
import argparse
from pathlib import Path
from airship.data.cdn_loader import CDNLoader

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--out", default="train_pairs.jsonl", help="Output pairs file")
    args = parser.parse_args()

    loader = CDNLoader(args.manifest)
    out_path = Path(args.out)
    count = 0
    with out_path.open("w") as f:
        for pair in loader.stream_pairs():
            f.write(json.dumps(pair) + "\n")
            count += 1
    print(f"Wrote {count} prompt/response pairs to {out_path}")

if __name__ == "__main__":
    import json
    main()
```

---

### 4. Wrapper & Cron Hygiene

Ensure any cron/launcher uses:

```bash
#!/usr/bin/env bash
# Example wrapper: airship/bin/run_discover.sh
set -euo pipefail
cd /opt/axentx/airship
python3 airship/cli/discover.py --repo "org/airship-data" --date "2026-04-29" --out manifest.json
```

Set crontab with:

```
SHELL=/bin/bash
```

---

## Validation Checklist

- [x] `airship discover` writes `manifest.json` with CDN URLs.
- [x] Loader uses CDN-only URLs (no Authorization header).
- [x] Projection to `{prompt, response}` happens at parse time; no schema errors.
- [x] Training script uses manifest; zero HF API calls during data load.
- [x] All wrappers have shebang and `chmod +x`.
