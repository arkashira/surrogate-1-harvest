# airship / discovery

**Final consolidated implementation** (best parts merged, contradictions resolved in favor of correctness + concrete actionability)

---

## What we ship (≤2h)

A **deterministic, CDN-only `airship discover` + train orchestrator** that:

- Eliminates HF API rate limits during discovery **and** training  
- Avoids PyArrow `CastError` from mixed-schema repos  
- Produces a reproducible `file-list.json` consumed by training with **zero HF API calls at runtime**  
- Reuses a running Lightning Studio to save quota  
- Uses only CDN fetches (`https://huggingface.co/datasets/.../resolve/main/...`)  
- Projects heterogeneous files to `{prompt, response}` at parse time (never loads full dataset with `load_dataset`)  

---

## Implementation plan (timeboxed)

### 1) Add `airship/cli/discover.py` — 20 min
- Single `list_repo_tree` call for one date folder  
- Save relative paths to `file_list.json`  
- Cache valid for 24 h (skip if fresh file exists)  

### 2) Add `airship/train/project.py` — 15 min
- `project_to_prompt_response(raw_bytes, filename) -> {prompt, response, filename}`  
- Detect schema by extension/content; extract only prompt/response  
- Never call `load_dataset` on the repo (avoids CastError)  

### 3) Update `airship/train/train.py` — 30 min
- Accept `--file-list-json` and `--repo`  
- Build CDN URLs and stream files with retries  
- Map each file through `project_to_prompt_response`  
- Create `datasets.Dataset` from list of dicts  

### 4) Add `airship/orchestrator/run_discover_and_train.sh` — 10 min
- Runs discover → reuses/starts studio → launches training  
- Shebang `#!/usr/bin/env bash`, `set -euo pipefail`  

### 5) Lightning Studio reuse guard — 10 min
- List studios; reuse running studio with matching name  
- Else create `Machine.L40S` (fallback to L40S if H200 unavailable)  

### 6) CDN URL builder — 5 min
- `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
- No Authorization header (bypasses API rate limits)  

### 7) Tests / smoke — 20 min
- Run orchestrator locally (Mac)  
- Verify no HF API calls during training (check logs)  
- Confirm no CastError on mixed-schema files  

---

## Code (final merged versions)

### `airship/cli/discover.py`
```python
#!/usr/bin/env python3
import json
import argparse
import time
from pathlib import Path
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)  # e.g. 2026-05-02
    parser.add_argument("--out", default="file_list.json")
    parser.add_argument("--cache-ttl", type=int, default=86400)
    args = parser.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and (time.time() - out_path.stat().st_mtime) < args.cache_ttl:
        print(f"Using cached file list: {out_path}")
        return

    api = HfApi()
    # List one date folder (non-recursive) to keep discovery fast and deterministic
    folder_path = f"{args.date}"
    tree = api.list_repo_tree(repo_id=args.repo, path=folder_path, recursive=False)

    files = [entry.path for entry in tree if entry.type == "file"]
    out_path.write_text(json.dumps(files, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

### `airship/train/project.py`
```python
import io
import json
from typing import Dict, Any

def project_to_prompt_response(raw_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Project heterogeneous HF dataset files to {prompt, response}.
    Avoids load_dataset() on mixed schemas.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    content = raw_bytes.decode("utf-8", errors="replace").strip()

    # JSONL / JSON: prefer standard fields
    if ext in ("jsonl", "json"):
        try:
            if ext == "jsonl":
                first_line = content.splitlines()[0].strip()
                data = json.loads(first_line) if first_line else {}
            else:
                data = json.loads(content)

            prompt = data.get("prompt") or data.get("input") or data.get("question") or ""
            response = data.get("response") or data.get("output") or data.get("answer") or ""
            return {"prompt": str(prompt), "response": str(response), "filename": filename}
        except Exception:
            # Fall through to text heuristics
            pass

    # Plain text: treat first non-empty block as prompt, remainder as response
    blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
    if len(blocks) >= 2:
        return {"prompt": blocks[0], "response": "\n\n".join(blocks[1:]), "filename": filename}

    # Fallback: entire content as prompt, empty response
    return {"prompt": content, "response": "", "filename": filename}
```

---

### `airship/train/train.py` (excerpt)
```python
import argparse
import json
import time
import requests
from pathlib import Path
from datasets import Dataset
from airship.train.project import project_to_prompt_response

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def _get_with_retry(url: str, retries: int = 3, backoff: float = 1.0) -> bytes:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == retries:
                raise
            time.sleep(backoff * attempt)
    raise RuntimeError("unreachable")

def load_via_cdn(file_list: list[str], repo: str) -> Dataset:
    rows = []
    for rel_path in file_list:
        url = CDN_TEMPLATE.format(repo=repo, path=rel_path)
        raw = _get_with_retry(url)
        row = project_to_prompt_response(raw, rel_path)
        rows.append(row)
    return Dataset.from_list(rows)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list-json", required=True)
    parser.add_argument("--repo", default="surrogate-data")
    args = parser.parse_args()

    file_list = json.loads(Path(args.file_list_json).read_text())
    dataset = load_via_cdn(file_list, args.repo)
    print(f"Loaded {len(dataset)} examples via CDN (zero HF API calls during load)")

    # Continue training with dataset...
    # trainer = ...
```

---

### `airship/orchestrator/run_discover_and_train.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="surrogate-data"
DATE="2026-05-02"
FILE_LIST="file_list.json"

cd "$(dirname "$0")/../.."

echo "=== Step 1: Discover (CDN-safe file list) ==="
python -m airship.cli.discover --repo "$REPO" --date "$DATE" --out "$FILE_LIST"

echo "=== Step 2: Reuse or start Lightning Studio ==="
python - <<'PY'
from lightning_sdk import Teamspace, Studio, Machine

team = Teamspace()
studio_name = "airship-train-l40s"
running = [s for s in team.studios if s.name == studio_name and s.status == "Running"]

if running:
    studio = running[0]
    print(f"Reusing running studio: {studio_name}")
else:
    studio = Studio.create(
        name=studio_name,

