# vanguard / quality

### Final Synthesized Solution  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

---

## 1. Diagnosis (merged + resolved)

- **No persisted `(repo, dateFolder)` manifest** → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.  
  *Resolution: require manifest generation as a pre-flight step; never re-enumerate during training.*

- **Data loader uses recursive enumeration or `load_dataset(..., streaming=True)`** → triggers `pyarrow.CastError` on heterogeneous repos and amplifies API calls.  
  *Resolution: ban recursive enumeration/`load_dataset` during training; use CDN-only, schema-stable loading from manifest.*

- **Lightning Studio reuse not enforced** → each run may create a new studio and burn quota; idle-stop kills long-running training.  
  *Resolution: enforce single-studio reuse + automatic idle-restart in launcher; never create ad-hoc studios inside training script.*

- **Training relies on authenticated HF API during data loading** instead of CDN-only fetches → avoidable rate-limit exposure.  
  *Resolution: data loader must use only CDN URLs from manifest; no `huggingface_hub` calls in training loop.*

- **Insufficient retries and error handling** for HF API calls during manifest generation.  
  *Resolution: exponential backoff + jitter for 429/5xx; hard retry limit; fail-fast on 4xx.*

---

## 2. Proposed change (high-leverage, minimal surface)

Create two artifacts and modify one:

1. `/opt/axentx/vanguard/scripts/generate_manifest.py` (new) — pre-flight manifest generator, CDN-only output.  
2. `/opt/axentx/vanguard/train.py` (modify) — replace data-loading section with CDN-only `IterableDataset` from manifest; remove any `load_dataset`/recursive enumeration.  
3. `/opt/axentx/vanguard/launch_studio.py` (modify) — enforce studio reuse and idle-restart logic; keep training script studio-agnostic.

Scope strictly limited to these files; no speculative Lightning Studio changes inside `train.py`.

---

## 3. Implementation

### 3.1 generate_manifest.py

```python
#!/usr/bin/env python3
"""
Generate and persist a (repo, dateFolder) manifest once.

Usage:
  python generate_manifest.py \
    --repo datasets/your-org/your-repo \
    --date-folder 2026-04-29 \
    --out manifests/2026-04-29_manifest.json
"""

import argparse
import json
import time
import random
from pathlib import Path

from huggingface_hub import HfApi, list_repo_tree
from huggingface_hub.utils import HFValidationError

MAX_RETRIES = 5
RETRY_BACKOFF = 30  # seconds (base)

def _retry(fn):
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                # Do not retry client errors (4xx) except 429
                if hasattr(exc, "response") and exc.response is not None:
                    status = exc.response.status_code
                    if 400 <= status < 500 and status != 429:
                        raise
                wait = RETRY_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 1)
                print(f"Attempt {attempt}/{MAX_RETRIES} failed: {exc}. Retrying in {wait:.1f}s")
                time.sleep(wait)
        raise last_exc
    return wrapper

@_retry
def _list_tree_safe(api, repo, path):
    return list_repo_tree(repo=repo, path=path, recursive=False)

def build_manifest(repo: str, date_folder: str, out_path: Path):
    api = HfApi()
    prefix = f"{date_folder}/"

    tree = _list_tree_safe(api, repo, prefix)

    files = []
    for entry in tree:
        if entry.type == "file":
            cdn_url = (
                f"https://huggingface.co/datasets/{repo}/resolve/main/{entry.path}"
            )
            files.append(
                {
                    "path": entry.path,
                    "cdn_url": cdn_url,
                    "size": getattr(entry, "size", None),
                }
            )

    if not files:
        raise ValueError(f"No files found for repo={repo}, date_folder={date_folder}")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date-folder", required=True, help="Folder in repo")
    parser.add_argument(
        "--out",
        default="manifests/latest_manifest.json",
        help="Output JSON path",
    )
    args = parser.parse_args()

    build_manifest(args.repo, args.date_folder, Path(args.out))
```

---

### 3.2 Modify train.py (data-loading section only)

Replace any authenticated HF API calls / `load_dataset` / recursive enumeration with:

```python
# In train.py
import json
import random
from pathlib import Path
from torch.utils.data import IterableDataset
import requests


class CDNTextDataset(IterableDataset):
    """
    Load text data from CDN URLs listed in a pre-generated manifest.
    Each file is expected to be newline-delimited JSON with at least:
      {"prompt": "...", "response": "..."}
    """

    def __init__(self, manifest_path: str, shuffle: bool = True):
        manifest_path = Path(manifest_path)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")

        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        self.urls = [f["cdn_url"] for f in manifest["files"]]
        if not self.urls:
            raise ValueError("No files in manifest")
        self.shuffle = shuffle
        self._rng = random.Random(42)

    def __iter__(self):
        urls = list(self.urls)
        if self.shuffle:
            self._rng.shuffle(urls)

        for url in urls:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                # Log and skip bad CDN assets to avoid crashing training
                print(f"Failed to fetch {url}: {exc}")
                continue

            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    prompt = item.get("prompt")
                    response = item.get("response")
                    if isinstance(prompt, str) and isinstance(response, str):
                        yield {"prompt": prompt, "response": response}
                except json.JSONDecodeError:
                    continue
```

**Actionable checklist for train.py integration:**
- Remove any `load_dataset(..., streaming=True)` or `list_repo_tree`/`HfApi` calls from training code.
- Instantiate dataset as:
  ```python
  train_dataset = CDNTextDataset("manifests/2026-04-29_manifest.json", shuffle=True)
  ```
- Ensure DataLoader uses `num_workers=0` if running in Lightning to avoid fork/resource issues, or properly configure persistent workers.

---

### 3.3 Modify launch_studio.py (studio lifecycle only)

Keep training script free of studio management. In launcher:

```python
import lightning as L
import time

def get_or_create_studio(name: str, idle_timeout_minutes: int = 30):
    """
