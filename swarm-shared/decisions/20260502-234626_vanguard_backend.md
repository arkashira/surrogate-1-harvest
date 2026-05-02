# vanguard / backend

Below is the **single, authoritative synthesis** that merges the strongest, non-redundant insights from both proposals, resolves contradictions in favor of **correctness + concrete actionability**, and provides a minimal, production-ready path forward.

---

## 1. Unified Diagnosis (root causes)

- **No persisted file manifest** → every training run re-calls `list_repo_tree`/`list_repo_files` against the HF Hub API, risking 429 (1000 req/5 min) and wasting quota.
- **Training loader uses HF dataset helpers or repeated auth checks** instead of direct CDN URLs → tighter rate limits and avoidable latency.
- **Lifecycle is recreate-on-each-run** (Lightning Studio) → cold-start latency and repeated bootstrap costs.
- **No local cache layer for repo file lists** → identical queries trigger repeated network round-trips.
- **Missing orchestration wrapper** to inject a pre-fetched manifest into training jobs → training cannot run CDN-only after bootstrap.

---

## 2. Unified Proposed Change (scope + boundaries)

Add a backend bootstrap module that:
- Persists repo file lists to local JSON (`vanguard/backend/manifests/{repo}/{date_slug}.json`) after **one HF API call**.
- Generates a **CDN-only file URL list** for training.
- Exposes a small FastAPI endpoint (`/bootstrap-manifest`) to fetch or create manifest (optional but useful for UI/orchestration).
- Updates the training launcher to accept a manifest path and use **CDN URLs exclusively** during data loading.

**Explicit scope**
- Create `vanguard/backend/bootstrap.py`
- Create `vanguard/backend/manifests/` (gitignored)
- Update/create `vanguard/backend/train_surrogate.py` to accept `--manifest` and use CDN URLs.
- Add `vanguard/backend/api.py` with one endpoint (optional).

**Explicit non-scope**
- Do not modify HF dataset configs or add new secrets.
- Do not change model architecture; keep training loop minimal and robust.

---

## 3. Resolved Contradictions (in favor of correctness + actionability)

| Contradiction | Resolution |
|--------------|------------|
| Candidate 1 uses `list_repo_tree(... recursive=False)` on a date folder; Candidate 2 implies recursive listing may be needed. | Use **non-recursive listing on the date folder** for bootstrap (fast, low-rate), then allow training script to stream files. If deeper trees are required, caller can recurse explicitly; default remains non-recursive to minimize API calls and avoid accidental huge listings. |
| Candidate 1 includes `hf_hub_download` import but doesn’t use it; Candidate 2 omits it. | Remove unused imports. Use **CDN URLs only** for training data fetch. Keep `hf_hub_download` out of hot path to avoid auth/rate costs. |
| Both propose optional FastAPI endpoint; Candidate 1 marks it optional, Candidate 2 does not emphasize orchestration. | Keep endpoint **lightweight and optional**, but include it because it enables Mac/local orchestration and UI bootstrap without CLI changes. |
| Candidate 1 uses deterministic hash in slug; Candidate 2 does not specify. | Keep deterministic slug (hash) to avoid collisions and enable idempotent bootstraps. |
| Candidate 1’s dataset assumes JSONL with `{prompt,response}`; Candidate 2 is silent on projection. | Keep projection **explicit and minimal**, but document that users must adapt `_stream_files` parsing to their schema. Provide a simple hook/override pattern. |

---

## 4. Final Implementation (single coherent codebase)

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/backend/manifests
touch /opt/axentx/vanguard/backend/__init__.py
```

### `/opt/axentx/vanguard/backend/bootstrap.py`
```python
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from huggingface_hub import list_repo_tree

MANIFESTS_ROOT = Path(__file__).parent / "manifests"


def repo_date_slug(repo: str, date_folder: str) -> str:
    key = f"{repo}::{date_folder}"
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    safe_repo = repo.replace("/", "_")
    return f"{safe_repo}__{date_folder}__{h}"


def ensure_manifest(repo: str, date_folder: str, revision: str = "main") -> Dict:
    """
    Return manifest:
    {
      "repo": "...",
      "date_folder": "...",
      "revision": "...",
      "created_at": "...",
      "files": [{"path": "...", "cdn_url": "...", "size": ...}, ...]
    }
    """
    slug = repo_date_slug(repo, date_folder)
    manifest_path = MANIFESTS_ROOT / f"{slug}.json"

    if manifest_path.exists():
        return json.loads(manifest_path.read_text())

    # Single API call: non-recursive list of the date folder
    tree = list_repo_tree(repo=repo, revision=revision, path=date_folder, recursive=False)
    files: List[Dict] = []
    for entry in tree:
        if entry.type != "file":
            continue
        path = entry.path
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        files.append({
            "path": path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "revision": revision,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def download_via_cdn(file_entry: Dict, target_dir: str) -> str:
    """Download a single file using CDN URL (no auth). Returns local path."""
    import requests
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / Path(file_entry["path"]).name
    if local_path.exists():
        return str(local_path)

    resp = requests.get(file_entry["cdn_url"], timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)
    return str(local_path)
```

### `/opt/axentx/vanguard/backend/train_surrogate.py`
```python
import argparse
import json
from pathlib import Path
from typing import Dict, Iterator

import torch
from torch.utils.data import IterableDataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments


class CDNTextDataset(IterableDataset):
    """
    Dataset that streams files listed in manifest using CDN URLs only.
    Projects each file to {prompt, response} at parse time.
    Override `_project_line` to match your schema.
    """

    def __init__(self, manifest_path: str, max_files: int = -1):
        manifest = json.loads(Path(manifest_path).read_text())
        self.file_entries = manifest["files"]
        if max_files > 0:
            self.file_entries = self.file_entries[:max_files]

    def _project_line(self, line: str) -> Dict:
        line = line.strip()
        if not line:
            return {}
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return {}
        prompt = obj.get("prompt") or obj.get("input") or ""
        response = obj.get("response") or obj.get("output") or ""
        if prompt or response:
            return {"prompt": prompt, "response": response}
        return {}

    def _stream_files(self) -> Iterator[Dict]:
        import requests
        for entry in self.file_entries:
            resp = requests.get(entry["cdn_url"], timeout=60)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                projected = self._project_line(line)
                if projected:
                    yield projected

    def __iter__(self):
        return self._stream_files()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--model", default="meta-llama/Llama-3.2-1B-Instruct")
    parser.add_argument("--
