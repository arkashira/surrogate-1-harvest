# vanguard / discovery

## Final Synthesis (single authoritative answer)

**Root cause:** training workflows repeatedly call HF API (`list_repo_tree`, `load_dataset`) during data loading, exhausting quota with 429s, while Lightning Studio lifecycle is not reused and Mac is misused for compute.

**Single source of truth:**  
- **Mac role:** launcher/SDK only.  
- **Compute target:** Lightning Studio (L40S or higher).  
- **Data loading rule:** zero HF API calls during training; use CDN URLs only.  
- **Studio lifecycle rule:** reuse a running Studio; restart if stopped; never recreate unnecessarily.

---

## 1. Durable ingestion manifest (one-time, Mac)

Create `/opt/axentx/vanguard/discovery/manifest.py`:

```bash
#!/usr/bin/env bash
# Generate durable file manifest for a single date folder to avoid HF API pagination/429s.
# Usage: bash manifest.py <repo> <date_folder> [out_json]
# Example: bash manifest.py datasets/my-corpus 2026-05-02 manifest.json

set -euo pipefail
REPO="${1:-datasets/my-corpus}"
DATEFOLDER="${2:-2026-05-02}"
OUT="${3:-manifest.json}"

python3 - "$REPO" "$DATEFOLDER" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

def main(repo_id: str, folder: str, out_path: str):
    api = HfApi()
    # Single non-recursive call to avoid pagination.
    entries = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
    files = [e.path for e in entries if not e.path.endswith("/")]
    manifest = {
        "repo_id": repo_id,
        "folder": folder,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo_id}/resolve/main"
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
PY
```

**Verification (Mac):**
```bash
cd /opt/axentx/vanguard/discovery
bash manifest.py datasets/my-corpus 2026-05-02 manifest.json
head=$(jq -r '.cdn_prefix + "/" + .files[0]' manifest.json)
curl -I "$head"   # expect HTTP 200 (no auth)
```

---

## 2. Lightning Studio launcher with reuse + restart

Create `/opt/axentx/vanguard/discovery/train_launcher.py`:

```python
#!/usr/bin/env python3
"""
Launcher for Surrogate-1 training on Lightning AI with CDN-only data fetches.
- Reuses running Studio to save quota.
- Restarts if stopped (idle timeout kills training).
- Embeds manifest so training uses CDN URLs only (zero HF API calls during load).
"""
import json, os, sys, time
from pathlib import Path
from lightning import Studio, Teamspace, Machine

MANIFEST_PATH = Path(__file__).parent / "manifest.json"
TRAIN_SCRIPT = Path(__file__).parent / "train.py"
STUDIO_NAME = "vanguard-surrogate-train"
MACHINE = Machine.L40S  # fallback; use higher if available and permitted

def load_manifest():
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest missing: {MANIFEST_PATH}. Run manifest.py first.")
    with open(MANIFEST_PATH) as f:
        return json.load(f)

def get_or_create_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            return s
    return Studio(name=STUDIO_NAME, machine=MACHINE, create_ok=True)

def run_training():
    manifest = load_manifest()
    env = os.environ.copy()
    env["VANGUARD_MANIFEST"] = str(MANIFEST_PATH)

    studio = get_or_create_studio()
    if studio.status != "running":
        print(f"Studio {STUDIO_NAME} is {studio.status}; starting...")
        studio.start(machine=MACHINE)
        for _ in range(60):
            studio.refresh()
            if studio.status == "running":
                break
            time.sleep(10)
        else:
            raise RuntimeError("Studio failed to start.")

    job = studio.run(
        str(TRAIN_SCRIPT),
        env=env,
        cwd=str(Path(__file__).parent),
    )
    print(f"Started training job: {job}")
    return job

if __name__ == "__main__":
    run_training()
```

---

## 3. Training script that uses CDN only (minimal, correct)

Create `/opt/axentx/vanguard/discovery/train.py`:

```python
import os, json, torch
from torch.utils.data import Dataset, DataLoader
import requests

class CDNTextDataset(Dataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.base = self.manifest["cdn_prefix"]
        self.files = self.manifest["files"]

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        url = f"{self.base}/{self.files[idx]}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Replace with actual parsing (parquet/jsonl -> {prompt,response}) as needed.
        text = resp.text
        return {"text": text, "url": url}

def main():
    manifest_path = os.environ.get("VANGUARD_MANIFEST", "manifest.json")
    ds = CDNTextDataset(manifest_path)
    loader = DataLoader(ds, batch_size=8, num_workers=0)
    for batch in loader:
        print(batch["url"][0], len(batch["text"][0]))
        break

if __name__ == "__main__":
    main()
```

**Make executable:**
```bash
chmod +x /opt/axentx/vanguard/discovery/manifest.py
chmod +x /opt/axentx/vanguard/discovery/train_launcher.py
```

---

## 4. Verification checklist (run on Mac)

1. Generate manifest:
   ```bash
   cd /opt/axentx/vanguard/discovery
   bash manifest.py datasets/my-corpus 2026-05-02 manifest.json
   ```
   Confirm `manifest.json` exists and lists files.

2. Confirm CDN accessibility (no auth):
   ```bash
   head=$(jq -r '.cdn_prefix + "/" + .files[0]' manifest.json)
   curl -I "$head"
   ```
   Expect HTTP 200 (no 401/403).

3. Launch training:
   ```bash
   python3 train_launcher.py
   ```
   Confirm:
   - Finds or creates a running Lightning Studio named `vanguard-surrogate-train`.
   - Studio status becomes “running” if previously stopped.
   - Training job starts and `train.py` loads files via CDN URLs (logs show CDN URLs; no HF API errors).

4. Validate zero API calls during load:
   - Monitor logs for `huggingface_hub`; none should appear during data loading.

---

## 5. Orchestration hygiene (non-negotiable)

- **Mac:** runs only `manifest.py` and `train_launcher.py`. No training compute.
- **Lightning Studio:** runs `train.py` and all heavy compute.
- **Model loading:** never call `model.from_pretrained()` on Mac; fetch model weights inside Studio (prefer CDN or local cache) or use Lightning’s built-in model facilities.
- **Quota protection:** manifest + CDN-only loading eliminates paginated API calls; Studio reuse prevents recreation burn.
