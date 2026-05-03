# surrogate-1 / frontend

## Final Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Highest-value improvement (≤2h)**  
Add `bin/snapshot.sh` that produces a deterministic file manifest per date folder and update ingestion/training to use CDN URLs exclusively when a snapshot is provided. This:

- Eliminates recursive `list_repo_files`/`list_repo_tree` calls during training (prevents 429s).  
- Enables Lightning training to do CDN-only fetches with zero HF API calls during data load.  
- Keeps the existing 16-shard runner unchanged (it already uses HF API on the Mac orchestrator, which is acceptable).  
- Reuses the existing `bin/dataset-enrich.sh` workflow so no retraining of infra is required.

---

### Concrete steps (order)

1. Add `bin/snapshot.sh`  
   - Accepts `REPO`, `DATE` (YYYY-MM-DD), optional `OUT_JSON`.  
   - Uses `huggingface_hub.list_repo_tree(path=f"{DATE}", recursive=False)` (single non-recursive call).  
   - Emits JSON array of `{ "path": "...", "cdn_url": "https://huggingface.co/datasets/REPO/resolve/main/...", "size": int }`.  
   - Deterministic sort by path.  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`.  
   - Executable (`chmod +x`).  

2. Add `bin/snapshot.py` (optional helper used by snapshot.sh)  
   - Pure Python to call HF Hub and emit JSON.  
   - Shebang `#!/usr/bin/env python3`, `if __name__ == "__main__"`.  
   - Handles pagination safely (non-recursive, single level).  

3. Update training script (`train.py` or equivalent)  
   - Accept `--snapshot JSON_PATH`.  
   - If provided, build a `WebDataset`/`IterableDataset` that streams from CDN URLs directly (no `load_dataset`).  
   - Project to `{prompt, response}` at parse time (preserve existing schema-projection behavior).  
   - Fallback: if no snapshot, keep current behavior but log warning about HF API usage.  

4. Update ingestion runner guidance (README or comment)  
   - Document how to produce snapshot before training.  
   - Note: Mac runs snapshot once per date folder after rate-limit window clears; Lightning training uses snapshot JSON (CDN-only).  

5. Small infra polish  
   - Ensure `requirements.txt` includes `huggingface_hub`, `webdataset` (if using WebDataset), `requests`.  
   - Add `.gitignore` entry for snapshot JSONs if needed.  

---

### Code snippets

#### `bin/snapshot.py`

```python
#!/usr/bin/env python3
"""
Produce a deterministic CDN manifest for a date folder in a HuggingFace dataset repo.

Usage:
  ./bin/snapshot.py --repo axentx/surrogate-1-training-pairs --date 2026-04-29 --out snapshot.json
"""

import argparse
import json
import sys
from pathlib import Path

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN snapshot for a dataset date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="snapshot.json", help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    prefix = args.date.rstrip("/") + "/"
    entries = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)

    files = []
    for e in entries:
        if getattr(e, "type", None) == "file" or (hasattr(e, "path") and not e.path.endswith("/")):
            path = e.path
            files.append(
                {
                    "path": path,
                    "cdn_url": CDN_TEMPLATE.format(repo=args.repo, path=path),
                    "size": getattr(e, "size", 0),
                }
            )

    files.sort(key=lambda x: x["path"])

    out_path = Path(args.out)
    out_path.write_text(json.dumps(files, indent=2) + "\n")
    print(f"Wrote {len(files)} files to {out_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

#### `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
#
# snapshot.sh — deterministic CDN manifest for a dataset date folder
#
# Usage:
#   ./bin/snapshot.sh --repo axentx/surrogate-1-training-pairs --date 2026-04-29 [--out snapshot.json]
#
# Requires: python3, huggingface_hub

set -euo pipefail

REPO=""
DATE=""
OUT="snapshot.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out)  OUT="$2";  shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" ]]; then
  echo "Usage: $0 --repo <repo> --date <YYYY-MM-DD> [--out <json>]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/snapshot.py"

if [[ ! -x "$PY_SCRIPT" ]]; then
  echo "Python snapshot helper not found or not executable: $PY_SCRIPT" >&2
  exit 1
fi

exec python3 "$PY_SCRIPT" --repo "$REPO" --date "$DATE" --out "$OUT"
```

#### Minimal CDN-only dataset reader snippet (to be integrated into `train.py`)

```python
import json
import random
from typing import Iterator, Dict, Any

import requests
import torch
from torch.utils.data import IterableDataset

class CDNTextDataset(IterableDataset):
    """
    Stream examples from CDN URLs listed in a snapshot JSON.
    Each file is expected to be JSONL with at least {prompt, response}.
    """

    def __init__(self, snapshot_path: str, shuffle_urls: bool = True):
        with open(snapshot_path) as f:
            self.files = json.load(f)
        if shuffle_urls:
            # Deterministic shuffle per worker can be added via seed if needed
            random.shuffle(self.files)

    def _stream_file(self, url: str) -> Iterator[Dict[str, Any]]:
        # Streaming line-by-line to avoid loading entire file into memory
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                obj = json.loads(line)
                # Project to {prompt, response} at parse time (schema-agnostic)
                prompt = obj.get("prompt") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": prompt, "response": response}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for f in self.files:
            yield from self._stream_file(f["cdn_url"])
```

---

### Acceptance checklist (manual verification)

- [ ] `bin/snapshot.py` executable and produces valid JSON for a test date folder.  
- [ ] `bin/snapshot.sh` invokes Python helper correctly and respects CLI args.  
- [ ] Training script accepts `--snapshot` and uses CDN-only URLs when provided.  
- [ ] No `load_dataset(streaming=True)` or recursive `list_repo_files` calls in training path when snapshot is used.  
- [ ] README updated with snapshot usage example.  

---

### Notes & trade-offs

- Snapshot is point-in-time; if new files appear for the same date after snapshot, they won’t be included until next snapshot run. This is acceptable because training runs should be reproducible against a fixed manifest.  
- The 16-shard GitHub Actions runner continues
