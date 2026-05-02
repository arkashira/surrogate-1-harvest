# vanguard / quality

Below is the **single, merged, corrected, and action-ready** synthesis.  
Contradictions were resolved in favor of correctness and deployability.

---

## 1. Unified Diagnosis (resolved)

- **HF CDN-bypass missing**: training uses `load_dataset`/HF API at runtime → 429s on heterogeneous repos.  
  **Fix**: generate `file-list.json` once (date-folder scoped) and use CDN URLs only during training.

- **Lightning Studio lifecycle non-idempotent**: scripts create new studios instead of reusing running ones → quota waste + idle-stop kills training.  
  **Fix**: detect existing studio; reuse if running; restart if stopped; never recreate blindly.

- **No pre-flight file-list step**: ingestion/training rely on runtime API pagination (rate-limited).  
  **Fix**: add deterministic `generate_file_list.py` executed before training (Mac/CI-friendly).

- **Mixed-schema load risk**: `pyarrow.CastError` when loading via streaming.  
  **Fix**: project to `{prompt, response}` at parse time (CDN fetch) and never rely on HF streaming during training.

- **Training script incomplete/cut off** in Candidate 1.  
  **Fix**: provide complete, runnable `train.py` with CDN-only dataset, deterministic collation, and studio-aware launcher.

---

## 2. Implementation

### 2.1 Create scripts directory

```bash
mkdir -p /opt/axentx/vanguard/scripts
```

---

### 2.2 `/opt/axentx/vanguard/scripts/generate_file_list.py`

```python
#!/usr/bin/env python3
"""
Generate file-list.json for a given HF dataset repo + date folder.
Run from Mac/CI after HF API rate-limit window clears.

Usage:
  HF_TOKEN=hf_xxx python generate_file_list.py \
    --repo datasets/your-org/your-repo \
    --date-folder 2026-05-02 \
    --out /opt/axentx/vanguard/file-list.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN file list for HF dataset repo.")
    parser.add_argument("--repo", required=True, help="HF repo (e.g., datasets/your-org/your-repo)")
    parser.add_argument("--date-folder", required=True, help="Date folder under repo root (e.g., 2026-05-02)")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)

    try:
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path=args.date_folder,
            recursive=False,
        )
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = [e.rfilename for e in entries if not e.rfilename.endswith("/")]
    if not files:
        print(f"No files found in {args.repo}/{args.date_folder}", file=sys.stderr)
        sys.exit(1)

    cdn_urls = [
        f"https://huggingface.co/datasets/{args.repo}/resolve/main/{f}"
        for f in files
    ]

    payload = {
        "repo": args.repo,
        "date_folder": args.date_folder,
        "files": files,
        "cdn_urls": cdn_urls,
    }

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Written {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 2.3 `/opt/axentx/vanguard/train.py`

```python
#!/usr/bin/env python3
"""
Surrogate-1 training script (Lightning).
- Uses CDN-only fetches via file-list.json (zero HF API calls during training).
- Projects mixed-schema files to {prompt, response} at parse time.
- Reuses running Lightning Studio; restarts if stopped.
"""
import json
import os
import sys
from pathlib import Path

import lightning as L
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoTokenizer

try:
    from transformers import AutoModelForCausalLM
except ImportError:
    AutoModelForCausalLM = None

# ---- Config ----
FILE_LIST_PATH = os.environ.get("FILE_LIST_PATH", "file-list.json")
MODEL_NAME = os.environ.get("MODEL_NAME", "microsoft/phi-2")  # example small model
MAX_LENGTH = int(os.environ.get("MAX_LENGTH", "1024"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2"))
EPOCHS = int(os.environ.get("EPOCHS", "1"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "outputs")
LR = float(os.environ.get("LR", "2e-5"))

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---- CDN Dataset (no HF API during training) ----
class CDNJsonlDataset(Dataset):
    """
    Load files via CDN URLs from file-list.json.
    Each line is JSON; project to {prompt, response} at parse time.
    """
    def __init__(self, file_list_path: str, tokenizer, max_length: int):
        self.tokenizer = tokenizer
        self.max_length = max_length

        with open(file_list_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.cdn_urls = manifest.get("cdn_urls", [])
        if not self.cdn_urls:
            raise ValueError("No CDN URLs found in file-list.json")

        self.examples = []
        import urllib.request
        for url in self.cdn_urls:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
            except Exception as exc:
                print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
                continue

            for line in raw.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
                if not prompt or not response:
                    continue
                self.examples.append((prompt, response))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        prompt, response = self.examples[idx]
        text = f"### Prompt:\n{prompt}\n\n### Response:\n{response}{self.tokenizer.eos_token}"
        enc = self.tokenizer(
            text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "labels": enc["input_ids"].squeeze(0),
        }

# ---- Studio reuse helpers ----
def get_or_create_studio(
    name: str,
    machine=L.Machine.L40S,
    create_ok: bool = True,
):
    """
    Reuse a running studio if exists; restart if stopped.
    Avoids recreating studios and respects Lightning quota.
    """
    teamspace = L.Teamspace()
    for studio in teamspace.studios:
        if studio.name == name:
            if studio.status == "running":
                print(f"Reusing running studio: {name}")
                return studio
            else:
                print(f"Studio {name} exists but status={studio.status}. Restarting...")
                studio.start(machine=machine)
                return studio
    if create_ok:
        print(f"Creating studio: {name}")
       
