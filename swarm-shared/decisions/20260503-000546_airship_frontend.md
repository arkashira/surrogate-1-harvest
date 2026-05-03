# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API 429s during Surrogate-1 training by replacing `load_dataset(streaming=True)` with a CDN-only `IterableDataset` that reads from a pre-generated `file_list.json`.

**Why this ships fast**:
- Single focused change in the data loader (no infra, no UI)
- Uses existing CDN bypass pattern (`resolve/main/` URLs)
- Removes recursive `list_repo_files` and streaming dataset calls that trigger 429s
- Embeds file list so Lightning training does zero API calls during data load

---

## Implementation Plan

### 1. Locate data loader code
Expected path: `/opt/axentx/airship/surrogate/data/` or `/opt/axentx/airship/surrogate/training/`  
Look for files containing `load_dataset`, `streaming=True`, or `IterableDataset`.

### 2. Create CDN-only IterableDataset
Replace HF streaming loader with a lightweight loader that:
- Accepts a `file_list.json` (generated once on Mac)
- Downloads each file via CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`)
- Parses `{prompt, response}` only at read time
- Yields samples indefinitely (shuffle across files)

### 3. Add file-list generator script (run once on Mac)
Single API call to `list_repo_tree(path, recursive=False)` for one date folder → save to JSON.

### 4. Wire into training script
Point training to new dataset class and pass `file_list.json`.

---

## Code Snippets

### `surrogate/data/cdn_dataset.py`
```python
import json
import random
from pathlib import Path
from typing import Iterator, List, Dict

import requests
from torch.utils.data import IterableDataset

CDN_BASE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CDNOnlyDataset(IterableDataset):
    """
    CDN-only dataset loader for Surrogate-1 training.
    Eliminates HF API calls during training by using pre-generated file_list.json
    and fetching via public CDN URLs (no Authorization header).
    """

    def __init__(
        self,
        repo: str,
        file_list_path: str,
        shuffle: bool = True,
        buffer_size: int = 1000,
    ):
        self.repo = repo
        self.file_list_path = Path(file_list_path)
        self.shuffle = shuffle
        self.buffer_size = buffer_size

        with open(self.file_list_path) as f:
            self.files: List[str] = json.load(f)

        if not self.files:
            raise ValueError("file_list.json is empty")

    def _fetch_file(self, rel_path: str) -> List[Dict]:
        url = CDN_BASE.format(repo=self.repo, path=rel_path)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        # Parquet/JSONL handling: project to {prompt, response} only
        # Lightweight parsing: assume JSONL lines with 'prompt' and 'response'
        samples = []
        for line in resp.text.strip().splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                samples.append(
                    {
                        "prompt": item.get("prompt", ""),
                        "response": item.get("response", ""),
                    }
                )
            except json.JSONDecodeError:
                continue
        return samples

    def _stream_files(self) -> Iterator[Dict]:
        indices = list(range(len(self.files)))
        if self.shuffle:
            random.shuffle(indices)

        while True:
            for idx in indices:
                rel_path = self.files[idx]
                try:
                    samples = self._fetch_file(rel_path)
                    if self.shuffle:
                        random.shuffle(samples)
                    for sample in samples:
                        if sample["prompt"] and sample["response"]:
                            yield sample
                except Exception as exc:
                    # Log and skip bad files; don't crash training
                    print(f"Skipping {rel_path}: {exc}")
                    continue

    def __iter__(self) -> Iterator[Dict]:
        return self._stream_files()
```

### `scripts/generate_file_list.py` (run once on Mac)
```python
import json
import os
from pathlib import Path

from huggingface_hub import HfApi

REPO = "your-org/surrogate-dataset"
DATE_FOLDER = "batches/mirror-merged/2026-04-29"  # adjust
OUTPUT = "file_list.json"

def main() -> None:
    api = HfApi()
    # Single non-recursive call per folder to avoid pagination/429
    tree = api.list_repo_tree(REPO, path=DATE_FOLDER, recursive=False)
    files = [
        f.rfilename
        for f in tree
        if f.rfilename.endswith((".jsonl", ".parquet"))
    ]

    # If parquet, we still list filenames; loader fetches via CDN
    out_path = Path(OUTPUT)
    out_path.write_text(json.dumps(files, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

### Usage in training script
```python
from surrogate.data.cdn_dataset import CDNOnlyDataset

dataset = CDNOnlyDataset(
    repo="your-org/surrogate-dataset",
    file_list_path="file_list.json",
    shuffle=True,
)

# Use with DataLoader (num_workers=0 recommended for CDN fetches)
loader = torch.utils.data.DataLoader(dataset, batch_size=8)
```

---

## Deployment Checklist
- [ ] Place `cdn_dataset.py` in `surrogate/data/`
- [ ] Run `generate_file_list.py` on Mac (after rate-limit window clears) → commit `file_list.json` or store in shared volume
- [ ] Update training script to use `CDNOnlyDataset`
- [ ] Remove any `load_dataset(streaming=True)` and recursive `list_repo_files` calls
- [ ] Test one training step locally (Lightning Studio or single GPU)

**Expected impact**: Zero HF API calls during training, no 429s, stable GPU utilization.
