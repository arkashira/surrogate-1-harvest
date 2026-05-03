# vanguard / quality

Below is the **single, consolidated implementation** that keeps the strongest, non-contradictory parts of both proposals and resolves every conflict in favor of **correctness + concrete actionability**.

---

## 1. Diagnosis (merged)
- No persisted manifest per `(repo, dateFolder)` → every training launch re-enumerates via authenticated HF API, burning quota and risking 429.
- Frontend cannot pre-flight or cache available files; users pick invalid/mismatched paths and training fails late.
- Training likely uses `load_dataset(streaming=True)` on heterogeneous repo files → `CastError` at runtime.
- Lightning Studio reuse is not enforced → new studio per run wastes quota.
- Data ingestion writes mixed-schema files with extra metadata columns instead of projecting to `{prompt, response}` only.

---

## 2. Proposed change (merged)
Create `/opt/axentx/vanguard/manifest.py` and `/opt/axentx/vanguard/train.py` (or patch existing) to:
- Add `build_manifest(repo, date_folder)` → writes `manifests/{repo_safe}/{date_folder}.json` listing file paths + CDN prefix (single API call).
- Use CDN-only downloads during training (zero authenticated API calls).
- Project each file to `{prompt, response}` at parse time (skip `load_dataset(streaming=True)`).
- Reuse running Lightning Studio by name; restart if idle-stopped.
- Add pre-flight validation against the manifest before training starts.

---

## 3. Implementation (final)

### `/opt/axentx/vanguard/manifest.py`
```bash
#!/usr/bin/env python3
"""
Build and cache per-(repo,date_folder) file manifests.
Usage:
  python3 manifest.py <repo> <date_folder> [--out-dir ./manifests]
"""
import json, os, sys
from pathlib import Path
from huggingface_hub import list_repo_tree

def build_manifest(repo: str, date_folder: str, out_dir: str = "./manifests") -> str:
    safe_repo = repo.replace("/", "_")
    out_path = Path(out_dir) / safe_repo / f"{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Single API call: non-recursive top-level of date folder
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = sorted(it.rfilename for it in items if it.type == "file")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    return str(out_path)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: manifest.py <repo> <date_folder> [--out-dir ./manifests]")
        sys.exit(1)
    repo, date_folder = sys.argv[1], sys.argv[2]
    out_dir = next(
        (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--out-dir" and i + 1 < len(sys.argv)),
        "./manifests",
    )
    p = build_manifest(repo, date_folder, out_dir)
    print(f"Manifest written: {p}")
```

---

### `/opt/axentx/vanguard/train.py`
```python
#!/usr/bin/env python3
"""
Train surrogate-1 with CDN-only fetches and Studio reuse.
Expects manifest at manifests/{repo_safe}/{date_folder}.json
"""
import json, os, sys, io
from pathlib import Path
from typing import List, Dict

import requests
import lightning as L
from lightning.fabric.plugins import BitsandbytesPrecision

# ---- config ----
REPO = os.getenv("HF_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
MANIFEST_PATH = Path("manifests") / REPO.replace("/", "_") / f"{DATE_FOLDER}.json"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "1000"))
STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-surrogate-train")

# ---- helpers ----
def load_manifest(path: Path) -> Dict:
    if not path.is_file():
        raise FileNotFoundError(f"Manifest missing: {path}. Run manifest.py first.")
    return json.loads(path.read_text())

def cdn_fetch(url: str) -> bytes:
    # CDN downloads do NOT count against authenticated API rate limits
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_file_to_qa(raw: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Extend per your file types. Examples:
      - JSONL with "prompt"/"response"
      - JSON with list of conversations
      - CSV with prompt,response columns
    """
    name = filename.lower()
    try:
        if name.endswith(".jsonl"):
            pairs = []
            for line in io.BytesIO(raw):
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                pairs.append({"prompt": obj["prompt"], "response": obj["response"]})
            return pairs

        if name.endswith(".json"):
            data = json.loads(raw)
            if isinstance(data, list):
                return [{"prompt": item["prompt"], "response": item["response"]} for item in data]
            return [{"prompt": data["prompt"], "response": data["response"]}]

        # Add CSV/TSV support as needed
        raise ValueError(f"Unsupported file type: {filename}")
    except Exception as e:
        raise ValueError(f"Failed to parse {filename}: {e}")

# ---- Lightning Studio reuse ----
def get_or_create_studio():
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            else:
                print(f"Restarting idle/stopped studio: {STUDIO_NAME}")
                s.start()
                return s

    print(f"Creating new studio: {STUDIO_NAME}")
    return teamspace.create_studio(
        name=STUDIO_NAME,
        # Adjust hardware as needed
        # hardware="gpu-l4",
        # count=1,
    )

# ---- dataset ----
class CDNQADataset:
    def __init__(self, manifest: Dict, max_files: int = -1):
        self.manifest = manifest
        files = manifest["files"]
        if max_files > 0:
            files = files[:max_files]
        self.urls = [f"{manifest['cdn_prefix']}/{f}" for f in files]

    def __len__(self):
        return len(self.urls)

    def __getitem__(self, idx):
        url = self.urls[idx]
        raw = cdn_fetch(url)
        pairs = parse_file_to_qa(raw, Path(url).name)
        return pairs

# ---- training step ----
def train_step(model, batch):
    # Replace with your actual surrogate-1 training step
    # This is a minimal placeholder
    loss = model.training_step(batch, batch_idx=0)
    return loss

# ---- main ----
def main():
    # Pre-flight: ensure manifest exists and is valid
    manifest = load_manifest(MANIFEST_PATH)
    print(f"Loaded manifest with {len(manifest['files'])} files")

    # Optional: reuse studio
    try:
        studio = get_or_create_studio()
        print(f"Studio ready: {studio.name}")
    except Exception as e:
        print(f"Studio reuse failed (continuing without): {e}")

    # Dataset and dataloader (simplified)
    dataset = CDNQADataset(manifest, max_files=100)
    # In practice, collate pairs into tokenized batches
    # Here we just demonstrate the pipeline

    # Fabric setup (example with bnb if needed)
    plugins = BitsandbytesPrecision("4bit", "nf4")
    fabric = L.Fabric(devices=1, precision="bf16-mixed
