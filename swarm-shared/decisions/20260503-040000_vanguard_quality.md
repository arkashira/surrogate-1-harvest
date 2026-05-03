# vanguard / quality

## Final Synthesis (Best Parts + Correctness + Actionability)

**Core diagnosis (unified):**  
- No content-addressed manifest → ingestion re-lists HF repos at runtime → 429 rate-limits and non-reproducible runs.  
- Mixed-schema files from `dataset-mirror` land in `enriched/` without projection to `{prompt,response}` → downstream `load_dataset` fails with schema errors.  
- Training uses HF Hub APIs/data loaders during training → burns auth rate limits instead of using raw CDN URLs.  
- Lightning quota waste from creating new Studios instead of reusing running ones.  
- No pre-flight file list → retries re-paginate and re-fail on 429.

**Single high-leverage fix (scope: 3 new files + 1 README snippet, <2h):**  
Create `/opt/axentx/vanguard/` with:

1. `scripts/build_file_manifest.py` — one-shot Mac-side script that lists a single date folder via `list_repo_tree`, saves `manifests/{date}.json`, and enforces `{prompt,response}` projection.  
2. `scripts/project_to_dialog.py` — filters/enforces schema from mirror files before upload to `enriched/`.  
3. `scripts/train_cdn_only.py` — Lightning training that loads only from CDN URLs listed in the manifest (zero API calls during training).  
4. `scripts/reuse_or_start_studio.py` — reuses a running Studio or starts one (L40S → fallback).

---

### Implementation

```bash
# /opt/axentx/vanguard/
mkdir -p scripts manifests enriched batches/mirror-merged
```

#### scripts/build_file_manifest.py
```python
#!/usr/bin/env python3
"""
Usage (Mac, after rate-limit window clears):
  HF_TOKEN=hf_xxx python scripts/build_file_manifest.py \
    --repo datasets/your-mirror \
    --date 2026-04-29 \
    --out manifests/2026-04-29.json
"""
import argparse
import json
import os
from pathlib import Path
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    # Single non-recursive call per date folder
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        repo_type="dataset",
        recursive=False,
    )

    files = [e.path for e in entries if e.type == "file"]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"date": args.date, "repo": args.repo, "files": files}, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

#### scripts/project_to_dialog.py
```python
#!/usr/bin/env python3
"""
Project heterogeneous mirror files to strict {prompt, response} pairs.
Usage:
  python scripts/project_to_dialog.py input.parquet output.parquet
"""
import pandas as pd
import sys
from pathlib import Path

def normalize_row(row):
    # Heuristic projection: accept common field names
    prompt = (
        row.get("prompt") or row.get("instruction") or row.get("input") or row.get("user") or ""
    )
    response = (
        row.get("response") or row.get("completion") or row.get("output") or row.get("assistant") or ""
    )
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def main():
    if len(sys.argv) != 3:
        print("Usage: project_to_dialog.py <input.parquet> <output.parquet>")
        sys.exit(1)

    inp, out = Path(sys.argv[1]), Path(sys.argv[2])
    df = pd.read_parquet(inp)
    projected = pd.DataFrame([normalize_row(r) for r in df.to_dict(orient="records")])
    # Drop empty
    projected = projected[(projected["prompt"].str.len() > 0) & (projected["response"].str.len() > 0)]
    out.parent.mkdir(parents=True, exist_ok=True)
    projected.to_parquet(out, index=False)
    print(f"Projected {len(projected)} rows -> {out}")

if __name__ == "__main__":
    main()
```

#### scripts/train_cdn_only.py
```python
#!/usr/bin/env python3
"""
Lightning training that loads exclusively from CDN (no HF API calls during training).
Requires: manifests/{date}.json produced by build_file_manifest.py
"""
import json
import os
from pathlib import Path
from typing import List

import torch
from torch.utils.data import IterableDataset, DataLoader
from lightning import LightningModule, Trainer
from lightning.pytorch.utilities.model_summary import ModelSummary
from transformers import AutoTokenizer, AutoModelForCausalLM

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path: str, base_url: str = "https://huggingface.co/datasets"):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.files = self.manifest["files"]
        self.base_url = base_url

    def _stream_file(self, path):
        url = f"{self.base_url}/{self.repo}/resolve/main/{path}"
        # Simple line-by-line JSONL reader; adapt to your file format
        import urllib.request, io, gzip
        try:
            with urllib.request.urlopen(url) as resp:
                # Try gzip, fallback raw
                try:
                    buf = gzip.GzipFile(fileobj=io.BytesIO(resp.read()))
                except OSError:
                    buf = io.BytesIO(resp.read())
                for line in io.TextIOWrapper(buf, encoding="utf-8"):
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        except Exception as e:
            print(f"Skipping {url}: {e}")

    def __iter__(self):
        for fpath in self.files:
            for obj in self._stream_file(fpath):
                # Expect {prompt, response}
                text = f"{obj['prompt']}\n\n{obj['response']}"
                yield text

class SimpleLM(LightningModule):
    def __init__(self, model_name="distilgpt2", lr=5e-5):
        super().__init__()
        self.model = AutoModelForCausalLM.from_pretrained(model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.lr = lr
        self.save_hyperparameters()

    def training_step(self, batch, batch_idx):
        inputs = self.tokenizer(batch, return_tensors="pt", truncation=True, max_length=512, padding=True)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        outputs = self.model(**inputs, labels=inputs["input_ids"])
        loss = outputs.loss
        self.log("train_loss", loss, prog_bar=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.lr)

def main():
    manifest = "manifests/2026-04-29.json"
    if not Path(manifest).exists():
        print(f"Manifest missing: {manifest}. Run build_file_manifest.py first.")
        return

    dataset = CDNTextDataset(manifest)
    loader = DataLoader(dataset, batch_size=4, collate_fn=lambda x: x)

    model = SimpleLM()
    trainer = Trainer(
        max_steps=200,
        accelerator="gpu" if torch.cuda.is_available() else "cpu",
        devices=1,
        log_every_n_steps=10,
    )
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

#### scripts/reuse_or_start_studio.py
```python
#!/usr/bin/env python3
"""
Reuse a running Studio or start a new one (L40S preferred, fallback to available).
Keeps
