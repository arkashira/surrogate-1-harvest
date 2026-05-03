# vanguard / quality

## Final Synthesized Implementation (Correct + Actionable)

Below is the single, consolidated plan that keeps the strongest, most correct parts from both proposals and resolves contradictions in favor of reliability, correctness, and concrete actionability.

---

## 1. Diagnosis (Consolidated)
- Frontend and backend both call authenticated HF API (`list_repo_tree`, `load_dataset`) on every preview/training launch → burns quota and risks 429s.
- No pre-listed file manifest: each run re-enumerates repo files via API instead of embedding a static file list for CDN-only fetches.
- No CDN-bypass path: data loading uses HF datasets client (auth + API) instead of raw CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`).
- No offline-first preview: frontend cannot render dataset samples without a live HF API token and network.
- No deterministic repo selection for commit-cap mitigation: writes could collide if scaled across repos.

---

## 2. Proposed Change (Consolidated)
Add a lightweight manifest-based CDN loader and offline preview for the training pipeline:

- Create `/opt/axentx/vanguard/training/manifest.py` — generates and embeds a static file list (date-folder scoped) for CDN-only fetches.
- Create `/opt/axentx/vanguard/training/cdn_loader.py` — streams parquet shards via CDN URLs without HF auth; projects to `{prompt, response}`.
- Create `/opt/axentx/vanguard/training/train.py` — uses manifest + CDN loader; accepts `--manifest` and `--repo` args.
- Create `/opt/axentx/vanguard/frontend/static/offline_preview.json` — small sample for offline-first UI preview.
- Update `/opt/axentx/vanguard/README.md` with usage and HF rate-limit/CDN notes.

Scope: ~200 lines total; <2h to ship.

---

## 3. Implementation

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/{training,frontend/static}
cd /opt/axentx/vanguard
```

### manifest.py
Generates a static file manifest for CDN-only dataset fetches. Run once per date-folder from any machine with HF token.

```python
"""
Generate and embed a static file manifest for CDN-only dataset fetches.
Run from Mac (or any machine with HF token) once per date-folder.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

REPO = os.getenv("HF_DATASET_REPO", "datasets/your-dataset")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "batches/mirror-merged/2026-05-03")
OUTPUT = os.getenv("MANIFEST_OUT", "training/manifest.json")

def build_manifest(repo: str, folder: str, out_path: str) -> None:
    entries = list_repo_tree(repo=repo, path=folder, recursive=True)
    files = [e.path for e in entries if e.type == "file" and e.path.lower().endswith(".parquet")]
    manifest = {
        "repo": repo,
        "folder": folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main"
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    build_manifest(REPO, DATE_FOLDER, OUTPUT)
```

### cdn_loader.py
CDN-only parquet loader. No HF auth. Uses manifest for file list.

```python
"""
CDN-only parquet loader. No HF auth. Uses manifest for file list.
"""
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import Iterator, Dict, Any
from pathlib import Path

class CDNLoader:
    def __init__(self, manifest_path: str):
        with open(manifest_path, encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.cdn_base = self.manifest.get("cdn_base") or "https://huggingface.co/datasets"
        self.repo = self.manifest["repo"]
        self.files = self.manifest["files"]

    def _stream_parquet(self, repo_path: str) -> bytes:
        url = f"{self.cdn_base}/{repo_path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def iter_rows(self, max_files: int = 0) -> Iterator[Dict[str, Any]]:
        count = 0
        for repo_path in self.files:
            if max_files and count >= max_files:
                break
            try:
                data = self._stream_parquet(repo_path)
                table = pq.read_table(BytesIO(data))
                # Project to {prompt, response} only
                cols = [c for c in table.column_names if c in ("prompt", "response")]
                if not cols:
                    # fallback: first two string/text cols
                    candidates = [c for c in table.column_names if table.schema.field(c).type in ("string", "large_string")]
                    cols = candidates[:2] if len(candidates) >= 2 else table.column_names[:2]
                batch = table.select(cols).to_pydict()
                keys = list(batch.keys())
                if len(keys) < 2:
                    continue
                k1, k2 = keys[0], keys[1]
                for i in range(len(batch[k1])):
                    yield {"prompt": batch[k1][i], "response": batch[k2][i]}
                count += 1
            except Exception as exc:
                # skip corrupt shard; log in prod
                continue
```

### train.py
Lightning-friendly training entrypoint using CDN manifest.

```python
"""
Lightning-friendly training entrypoint using CDN manifest.
"""
import argparse
import json
from pathlib import Path
from cdn_loader import CDNLoader

def build_dataset(manifest_path: str, max_files: int = 0):
    loader = CDNLoader(manifest_path)
    samples = list(loader.iter_rows(max_files=max_files))
    print(f"Loaded {len(samples)} samples from manifest")
    return samples

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="training/manifest.json")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--output", default="training/offline_dataset.jsonl")
    args = parser.parse_args()

    if not Path(args.manifest).exists():
        print(f"Manifest not found: {args.manifest}")
        print("Run: HF_DATASET_REPO=... HF_DATE_FOLDER=... python training/manifest.py")
        return

    samples = build_dataset(args.manifest, max_files=args.max_files)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"Dataset saved to {args.output}")

if __name__ == "__main__":
    main()
```

### frontend/static/offline_preview.json
Small sample for offline-first UI preview.

```json
{
  "description": "Offline-first preview sample for vanguard frontend",
  "generated_at": "2026-05-03T02:40:00Z",
  "samples": [
    {
      "prompt": "Summarize the key risks of CDN-bypass for dataset training.",
      "response": "Key risks: stale manifest (missing new files), CDN availability (rare), and schema drift if projection changes. Mitigations: regenerate manifest daily, validate schema on load, fallback to authenticated API when CDN fails."
    },
    {
      "prompt": "How should Lightning Studio reuse be handled to save quota?",
      "response": "List Teamspace.studios before creating; reuse running studios by name. If stopped, restart with target machine
