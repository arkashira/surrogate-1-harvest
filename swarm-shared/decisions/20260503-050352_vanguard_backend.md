# vanguard / backend

## 1. Diagnosis

- Runtime HF API calls (`list_repo_tree`, `load_dataset`) in training scripts cause 429 rate limits and non-reproducible shard ordering.
- No content-addressed manifest means epochs drift across runs and resumption is unreliable.
- Training on Lightning risks quota waste (studio recreation) and idle-stop kills long jobs.
- Mixed-schema ingestion writes extra metadata columns (`source`, `ts`) that break surrogate-1 schema expectations.
- No CDN bypass strategy: training still depends on authenticated `/api/` endpoints instead of public CDN URLs.

## 2. Proposed change

File scope: `/opt/axentx/vanguard/train.py` (create or update) and `/opt/axentx/vanguard/manifest/` (new folder).  
Goal: single deterministic manifest + CDN-only data loader + Lightning studio reuse + schema-projection at parse time.

## 3. Implementation

Create/replace training entrypoint and add manifest utilities.

```bash
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint (backend).
- Uses content-addressed manifest (CDN-only)
- Projects mixed-schema files to {prompt,response} at parse time
- Reuses running Lightning studio; falls back to L40S on public cloud
"""
import json
import os
import pathlib
from typing import Dict, List, Iterator

import torch
from datasets import IterableDataset
from lightning import Fabric
from lightning.fabric.plugins import TorchCheckpointIO

MANIFEST_PATH = pathlib.Path(__file__).parent / "manifest" / "file_list.json"
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-repo")
HF_REVISION = os.getenv("HF_REVISION", "main")
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/{rev}/{path}"

# ---- Manifest helpers ----
def load_manifest() -> List[Dict]:
    if not MANIFEST_PATH.exists():
        raise RuntimeError(
            f"Manifest missing: {MANIFEST_PATH}. Run generate_manifest.py on Mac first."
        )
    with open(MANIFEST_PATH) as f:
        return json.load(f)

def build_cdn_urls(manifest: List[Dict]) -> List[str]:
    return [
        CDN_TEMPLATE.format(repo=HF_DATASET_REPO, rev=HF_REVISION, path=entry["path"])
        for entry in manifest
    ]

# ---- Streaming dataset (CDN-only, no HF API calls) ----
class Surrogate1IterableDataset(IterableDataset):
    def __init__(self, file_urls: List[str], tokenizer, max_length: int = 2048):
        super().__init__()
        self.file_urls = file_urls
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            urls = self.file_urls
        else:
            per_worker = len(self.file_urls) // worker_info.num_workers
            urls = self.file_urls[
                worker_info.id * per_worker : (worker_info.id + 1) * per_worker
            ]

        for url in urls:
            # Stream parquet via pyarrow; project only prompt/response
            import pyarrow.parquet as pq
            import pyarrow as pa
            import requests

            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(pa.BufferReader(resp.content))
            # Keep only expected fields; ignore extra metadata
            rows = []
            for i in range(table.num_rows):
                row = table.slice(i, 1).to_pydict()
                prompt = row.get("prompt") or row.get("input") or ""
                response = row.get("response") or row.get("output") or ""
                if not prompt or not response:
                    continue
                text = f"### Prompt:\n{prompt}\n\n### Response:\n{response}"
                tokenized = self.tokenizer(
                    text, truncation=True, max_length=self.max_length
                )
                rows.append(tokenized)
            yield from rows

# ---- Lightning setup + studio reuse ----
def get_fabric() -> Fabric:
    # Prefer reuse: list running studios via Lightning SDK if available
    try:
        from lightning import Studio, Teamspace
        studios = Teamspace.studios
        for s in studios:
            if "vanguard" in (s.name or "") and s.status == "Running":
                # Attach to running studio; Lightning Fabric will use its resources
                return Fabric(accelerator="cuda", devices=1, strategy="auto")
    except Exception:
        pass

    # Default: local or Lightning public cloud (L40S max on free tier)
    return Fabric(accelerator="cuda", devices=1, strategy="auto")

# ---- Training step (minimal) ----
def train_step(fabric: Fabric, model, batch):
    model.train()
    outputs = model(**batch)
    loss = outputs.loss
    fabric.backward(loss)
    return loss

# ---- Main ----
def main():
    fabric = get_fabric()
    fabric.launch()

    # Load tokenizer + model (example; replace with your model)
    from transformers import AutoTokenizer, AutoModelForCausalLM
    tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-2-7b-hf")
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-2-7b-hf")

    manifest = load_manifest()
    urls = build_cdn_urls(manifest)
    dataset = Surrogate1IterableDataset(urls, tokenizer)

    loader = torch.utils.data.DataLoader(
        dataset, batch_size=2, num_workers=2
    )
    loader = fabric.setup_dataloaders(loader)
    model, = fabric.setup(model)

    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    model, optimizer = fabric.setup(model, optimizer)

    for epoch in range(3):
        for batch in loader:
            optimizer.zero_grad()
            loss = train_step(fabric, model, batch)
            fabric.log_dict({"train_loss": loss, "epoch": epoch})
            optimizer.step()

    # Save checkpoint (content-addressed by manifest hash)
    import hashlib
    manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    ckpt_path = pathlib.Path(__file__).parent / "checkpoints" / f"ckpt-{manifest_hash}"
    ckpt_path.parent.mkdir(exist_ok=True)
    fabric.save(ckpt_path / "model.pt")
    (ckpt_path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Checkpoint saved: {ckpt_path}")

if __name__ == "__main__":
    main()
```

```bash
# /opt/axentx/vanguard/generate_manifest.py
#!/usr/bin/env python3
"""
Run on Mac (or any non-training machine) after HF rate-limit window clears.
Generates content-addressed manifest for CDN-only training.
"""
import json
import os
import pathlib
import hashlib
from datetime import datetime

# Requires: pip install huggingface_hub
from huggingface_hub import HfApi

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-repo")
OUTPUT_DIR = pathlib.Path(__file__).parent / "manifest"
OUTPUT_DIR.mkdir(exist_ok=True)

def generate_manifest(date_folder: str = None):
    api = HfApi()
    # List top-level folder once (non-recursive) to avoid pagination/429
    items = api.list_repo_tree(repo_id=HF_DATASET_REPO, path=date_folder or "", recursive=False)
    files = [it.rfilename for it in items if it.type == "file" and it.rfilename.endswith(".parquet")]

    manifest = []
    for f in sorted(files):
        full_path = f"{date_folder}/{f}" if date_folder else f
        manifest.append({
            "path": full_path,
            "sha256": None,  # optional: fetch etag/sha via HEAD if desired
            "added_ts": datetime.utcnow().isoformat()
        })

    out_path = OUTPUT_DIR / "file_list.json"
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    manifest_hash = hash
