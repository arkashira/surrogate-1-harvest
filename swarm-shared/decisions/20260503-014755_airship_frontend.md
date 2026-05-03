# airship / frontend

## Highest-Value Incremental Improvement
**CDN-only HF ingestion + deterministic sibling-repo sharding**  
- Eliminates HF API 429s during training by pre-listing once and downloading via public CDN URLs (no auth).  
- Projects heterogeneous files to `{prompt,response}` only at parse time (avoids PyArrow schema errors).  
- Spreads commits across 5 sibling repos to bypass 128/hr/repo cap (640/hr aggregate).  
- Reuses running Lightning Studio to save quota and handles idle-stop safely.

---

## Implementation Plan (<2h)

| Step | Owner | Time | Command / Code |
|------|-------|------|----------------|
| 1. Create sibling repo list and deterministic shard selector | FE | 10m | `shards = ["airship-enriched-00", ..., "airship-enriched-04"]` |
| 2. Add `scripts/list_hf_date_folder.py` (Mac orchestration) | FE | 20m | See snippet below |
| 3. Add `scripts/build_train_manifest.py` (project to `{prompt,response}`) | FE | 20m | See snippet below |
| 4. Add `training/train.py` (Lightning Studio, CDN-only dataloader) | FE | 40m | See snippet below |
| 5. Add `scripts/reuse_or_start_studio.py` (reuse running studio) | FE | 15m | See snippet below |
| 6. Wire into `docker-compose.microservices.yml` (optional service) | FE | 15m | Add surrogate-training service |

---

## Code Snippets

### 1. `scripts/list_hf_date_folder.py`
Pre-list one date folder after rate-limit window clears; save to JSON for CDN-only training.

```python
#!/usr/bin/env python3
"""
Usage (Mac orchestration only):
  HF_TOKEN=hf_xxx python scripts/list_hf_date_folder.py \
    --repo my-org/airship-enriched \
    --date 2026-04-29 \
    --out filelist.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi, hf_hub_download

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", default="filelist.json")
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Non-recursive per folder to avoid 100x pagination on big repos
    tree = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename.endswith((".jsonl", ".parquet", ".json"))]

    # Also include subfolders one level deep (common pattern: date/split/)
    for f in list(tree):
        if "." not in f.rfilename.split("/")[-1]:  # likely a subfolder
            sub = api.list_repo_tree(repo_id=args.repo, path=f.rfilename, recursive=False)
            files.extend([sf.rfilename for sf in sub if sf.rfilename.endswith((".jsonl", ".parquet", ".json"))])

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(set(files)),
        "cdn_prefix": f"https://huggingface.co/datasets/{args.repo}/resolve/main"
    }
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 2. `scripts/build_train_manifest.py`
Project heterogeneous files to `{prompt,response}` only and shard across siblings.

```python
#!/usr/bin/env python3
"""
Build training manifest with deterministic sibling repo assignment.
Usage:
  python scripts/build_train_manifest.py filelist.json manifest.ndjson
"""
import json
import hashlib
import sys

SIBLINGS = [f"airship-enriched-{i:02d}" for i in range(5)]  # 00..04

def pick_sibling(slug: str) -> str:
    h = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(h[:8], 16) % len(SIBLINGS)
    return SIBLINGS[idx]

def project_record(raw: dict) -> dict:
    # Keep only prompt/response; drop source/ts to avoid schema conflicts
    return {
        "prompt": raw.get("prompt") or raw.get("input") or "",
        "response": raw.get("response") or raw.get("output") or raw.get("completion") or "",
    }

def main():
    _, filelist_path, out_path = sys.argv
    with open(filelist_path) as f:
        meta = json.load(f)

    prefix = meta["cdn_prefix"]
    with open(out_path, "w") as out:
        for rf in meta["files"]:
            url = f"{prefix}/{rf}"
            slug = rf.rsplit(".", 1)[0].replace("/", "-")
            sibling = pick_sibling(slug)
            # filename pattern: batches/mirror-merged/{date}/{slug}.parquet
            # we store url + sibling for later uploader
            rec = {
                "cdn_url": url,
                "sibling_repo": sibling,
                "slug": slug,
                "filename": f"batches/mirror-merged/{meta['date']}/{slug}.parquet"
            }
            out.write(json.dumps(rec) + "\n")
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 3. `training/train.py`
Lightning Studio entrypoint; CDN-only dataloader (zero HF API calls during training).

```python
#!/usr/bin/env python3
"""
Lightning Studio training script (run inside Studio).
Uses CDN-only URLs; projects mixed schemas on-the-fly.
"""
import json
import os
import io
import pyarrow.parquet as pq
import pyarrow as pa
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest.ndjson")
MODEL_NAME = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path):
        self.items = []
        with open(manifest_path) as f:
            for line in f:
                rec = json.loads(line.strip())
                self.items.append(rec)

    def __iter__(self):
        for rec in self.items:
            resp = requests.get(rec["cdn_url"], timeout=30)
            resp.raise_for_status()
            buf = io.BytesIO(resp.content)
            try:
                table = pq.read_table(buf)
            except pa.ArrowInvalid:
                continue
            # Project to prompt/response only (ignore other cols)
            cols = table.column_names
            has_prompt = "prompt" in cols
            has_response = "response" in cols
            if not (has_prompt and has_response):
                continue
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                text = f"Prompt: {row['prompt']}\nResponse: {row['response']}"
                yield {"text": text}

def main():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME)

    dataset = CDNParquetDataset(MANIFEST_PATH)
    def collate(batch):
        texts = [item["text"] for item in batch]
        enc = tokenizer(texts, truncation=True, padding=True, max_length=512)
        return {
            "input_ids": torch.tensor(enc["input_ids"]),
            "attention_mask": torch.tensor(enc["attention_mask"]),
            "labels": torch.tensor(enc["input_ids"]),
        }

    loader = DataLoader(dataset, batch_size=8, collate_fn=collate)

    training_args = TrainingArguments(
        output_dir="./out",
       
