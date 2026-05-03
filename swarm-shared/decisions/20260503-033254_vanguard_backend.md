# vanguard / backend

### Final synthesized solution (correct + actionable)

**Diagnosis (resolved)**  
- Scripts currently call `list_repo_tree`/`load_dataset` at runtime → 429 risk, quota burn, non-reproducible runs.  
- No deterministic, content-addressable file list keyed by date/slug → training jobs can’t replay exact data.  
- No CDN-first data path → backend still hits HF API during loading.  
- No lightweight orchestration to pin manifests before Lightning Studio runs → wasted quota and unreproducible jobs.

**Single proposed change**  
Add a backend manifest generator + CDN-first loader + Studio reuse helper that runs **once per date folder** and pins an exact file list (with optional sha256) so training jobs stream from CDN with zero HF API calls and can be deterministically replayed.

---

### Implementation (concrete, copy-ready)

```bash
# Create backend directory
mkdir -p /opt/axentx/vanguard/backend
cd /opt/axentx/vanguard/backend
```

#### 1) `generate_manifest.py` — run on orchestration host (Mac/CI) once per date folder
```python
#!/usr/bin/env python3
"""
Generate a CDN-first manifest for a date folder in a HF dataset repo.
Usage:
  python generate_manifest.py --repo datasets/myorg/surrogate-1 --date 2026-04-29 --out .
Produces: manifest-2026-04-29.json
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_dir: Path) -> Path:
    print(f"Listing repo tree for {repo}/{date_folder} ...")
    items = list_repo_tree(repo=repo, path=date_folder, recursive=True)

    files = []
    for item in items:
        if getattr(item, "type", None) != "file":
            continue
        path = getattr(item, "path", "")
        if not path:
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        files.append({
            "path": path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            # sha256 not provided by tree API; optional: fetch via HEAD ETag or compute on first use
            "sha256": None
        })

    if not files:
        raise RuntimeError(f"No files found for {repo}/{date_folder}")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "total_files": len(files),
        "strategy": "cdn-only",
        "notes": "Pin this file to replay exact training data. Training should use CDN URLs only."
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    slug = date_folder.replace("/", "_")
    out_path = out_dir / f"manifest-{slug}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN-first manifest for HF dataset date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. datasets/myorg/surrogate-1")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", default=".", help="Output directory for manifest")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

#### 2) `train_cdn.py` — Lightning Studio entrypoint (zero HF API calls during data load)
```python
#!/usr/bin/env python3
"""
Lightning training entrypoint that uses a pinned CDN manifest.
Usage in Studio:
  python train_cdn.py --manifest /opt/axentx/vanguard/backend/manifest-2026-04-29.json --max-files 10000
"""
import argparse
import json
from pathlib import Path
from typing import Iterator, Tuple

import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

try:
    import lightning as L
except ImportError:
    print("Install: pip install lightning")
    raise

class CDNTextDataset(IterableDataset):
    """
    Stream newline-delimited JSONL from CDN URLs listed in manifest.
    Expected line format: {"prompt": "...", "response": "..."}
    """
    def __init__(self, manifest_path: Path, max_files: int = None, shuffle_urls: bool = True):
        manifest = json.loads(manifest_path.read_text())
        urls = [f["cdn_url"] for f in manifest["files"] if f.get("cdn_url")]
        if max_files:
            urls = urls[:max_files]
        if shuffle_urls:
            import random
            rng = random.Random(42)
            rng.shuffle(urls)
        self.urls = urls

    def _stream_file(self, url: str) -> Iterator[Tuple[str, str]]:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                prompt = obj.get("prompt")
                response = obj.get("response")
                if isinstance(prompt, str) and isinstance(response, str):
                    yield prompt, response
            except Exception:
                continue

    def __iter__(self):
        for url in self.urls:
            yield from self._stream_file(url)

class SimpleTokenizer:
    def __init__(self):
        # Replace with real tokenizer in production
        pass
    def encode(self, text: str):
        return torch.tensor([ord(c) for c in text[:256]], dtype=torch.long)

class LitModel(L.LightningModule):
    def __init__(self, lr=1e-3):
        super().__init__()
        self.embed = torch.nn.Embedding(256, 64)
        self.lstm = torch.nn.LSTM(64, 128, batch_first=True)
        self.head = torch.nn.Linear(128, 256)
        self.lr = lr

    def training_step(self, batch, batch_idx):
        x, y = batch
        x = self.embed(x)
        _, (h, _) = self.lstm(x)
        logits = self.head(h[-1])
        loss = torch.nn.functional.cross_entropy(logits.view(-1, 256), y.view(-1))
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

def collate_fn(batch, tokenizer):
    xs, ys = [], []
    for prompt, response in batch:
        # simple example: predict response tokens from prompt tokens
        x_ids = tokenizer.encode(prompt)
        y_ids = tokenizer.encode(response)
        if len(x_ids) == 0 or len(y_ids) == 0:
            continue
        xs.append(x_ids)
        ys.append(y_ids)
    if not xs:
        return torch.zeros(1, 1, dtype=torch.long), torch.zeros(1, 1, dtype=torch.long)
    x = torch.nn.utils.rnn.pad_sequence(xs, batch_first=True, padding_value=0)
    y = torch.nn.utils.rnn.pad_sequence(ys, batch_first=True, padding_value=0)
    return x, y

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path, help="Path to pinned manifest JSON")
    parser.add_argument("--max-files", type=int, default=None, help="Limit files for quick tests")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr
