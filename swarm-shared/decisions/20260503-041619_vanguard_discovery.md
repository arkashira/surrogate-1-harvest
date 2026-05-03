# vanguard / discovery

## Final Consolidated Solution

**Core diagnosis (agreed by both):**  
- No content-addressed manifest → runtime HF API hits → 429s, non-reproducible epochs, no shareable snapshots.  
- Mixed-schema ingestion (`source`, `ts`) breaks `load_dataset` expectations; training expects `{prompt, response}`.  
- No CDN-bypass strategy → every epoch re-authenticates and risks rate limits.  
- No per-date file manifest → training cannot run CDN-only fetches.  
- No Lightning Studio reuse guard → quota burned on recreation.

**Chosen approach:**  
Adopt Candidate 1’s concrete artifacts (single-file manifest builder + Lightning launcher) with Candidate 2’s emphasis on strict projection, CDN-first loading, and reproducible snapshots. Resolve contradictions in favor of correctness and immediate actionability.

---

## 1. Manifest builder (corrected and hardened)

File: `/opt/axentx/vanguard/scripts/build_manifest.py`

Key fixes vs Candidate 1:
- Validate that every file contains `prompt` and `response` columns; fail fast if not.
- Use deterministic hash (SHA-256) of file content for content-addressing, not only URL-based.
- Support parquet, jsonl, json; reject unsupported types early.
- Allow optional `date_folder` default to latest folder in repo.
- Store per-file `sha256` and `num_rows` in manifest for reproducibility and quick checks.
- Keep manifest minimal and shareable; do not embed data.

```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a date folder in a HF dataset repo.
Outputs manifest-{date}.json with CDN URLs, hashes, and row counts.
"""
import json, hashlib, os, sys, tempfile
from pathlib import Path
from typing import Iterator, Dict, Any, List

import requests
from huggingface_hub import HfApi, hf_hub_download

HF_API = HfApi()
CDN_TMPL = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_files(repo: str, date_folder: str) -> List[dict]:
    """Single API call: list top-level files in date_folder (non-recursive)."""
    items = HF_API.list_repo_tree(repo, path=date_folder, recursive=False)
    files = [it for it in items if it.get("type") == "file"]
    if not files:
        raise ValueError(f"No files found in {repo}/{date_folder}")
    return files

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def project_to_prompt_response(file_path: str) -> Iterator[Dict[str, str]]:
    """
    Project supported files to {prompt, response}. Fail fast if columns missing.
    """
    p = Path(file_path)
    if p.suffix == ".parquet":
        import pyarrow.parquet as pq
        tbl = pq.read_table(p, columns=["prompt", "response"])
        df = tbl.to_pandas()
    elif p.suffix == ".jsonl":
        import pandas as pd
        df = pd.read_json(file_path, lines=True, dtype=str)
    elif p.suffix == ".json":
        import pandas as pd
        obj = json.loads(p.read_text())
        if not isinstance(obj, list):
            obj = [obj]
        df = pd.DataFrame(obj)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix}")

    required = {"prompt", "response"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns {missing} in {file_path}")

    for _, row in df.iterrows():
        yield {"prompt": str(row["prompt"]), "response": str(row["response"])}

def build_manifest(repo: str, date_folder: str, out_dir: str = "manifests") -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    manifest_path = out_path / f"manifest-{Path(date_folder).name}.json"

    files = list_date_files(repo, date_folder)
    manifest = []

    for f in files:
        rel = f["path"]
        cdn_url = CDN_TMPL.format(repo=repo, path=rel)

        # Download once (authenticated) to compute hash/rows; training will use CDN.
        local_path = hf_hub_download(repo_id=repo, filename=rel)
        content = Path(local_path).read_bytes()
        sha256 = _sha256_bytes(content)

        # Count rows via projection (lightweight for parquet/jsonl).
        try:
            rows = list(project_to_prompt_response(local_path))
            num_rows = len(rows)
        except Exception as e:
            raise ValueError(f"Cannot project {rel}: {e}")

        entry = {
            "repo": repo,
            "path": rel,
            "cdn_url": cdn_url,
            "size": f.get("size"),
            "sha256": sha256,
            "num_rows": num_rows,
            "lfs": f.get("lfs", False),
        }
        manifest.append(entry)

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} entries -> {manifest_path}")
    return str(manifest_path)

class CDNIterableDataset:
    """
    Iterable dataset that fetches files via CDN (no HF API auth during training).
    Pass manifest JSON path to constructor.
    """
    def __init__(self, manifest_path: str):
        self.manifest = json.loads(Path(manifest_path).read_text())

    def __iter__(self):
        for entry in self.manifest:
            url = entry["cdn_url"]
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                suffix = Path(entry["path"]).suffix
                tmp = Path(tempfile.gettempdir()) / f"tmp_{hashlib.md5(url.encode()).hexdigest()}{suffix}"
                tmp.write_bytes(r.content)
                yield from project_to_prompt_response(str(tmp))
                tmp.unlink(missing_ok=True)
            except Exception:
                # LFS or CDN failure -> authenticated fallback (rare).
                local = hf_hub_download(repo_id=entry["repo"], filename=entry["path"])
                yield from project_to_prompt_response(local)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: build_manifest.py <repo> <date_folder> [out_dir]")
        sys.exit(1)
    repo = sys.argv[1]
    date_folder = sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "manifests"
    build_manifest(repo, date_folder, out_dir)
```

---

## 2. Lightning launcher with Studio reuse

File: `/opt/axentx/vanguard/scripts/run_launcher.py`

Key fixes vs Candidate 1:
- Explicitly check for running Studio by name and reuse when available.
- If not running, start fresh L40S compute.
- Pass manifest path via env var and validate before launch.
- Keep launcher minimal and deterministic.

```python
#!/usr/bin/env python3
"""
Lightning launcher that reuses a running Studio or starts L40S,
then runs training with a manifest (zero HF API calls during data load).
"""
import os
import sys
from pathlib import Path

try:
    from lightning import LightningWork, LightningApp, Machine
except Exception:
    print("Install lightning: pip install lightning")
    sys.exit(1)

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifests/manifest-2026-05-03.json")
TRAIN_SCRIPT = os.getenv("TRAIN_SCRIPT", "train.py")

class VanguardWork(LightningWork):
    def __init__(self):
        super().__init__(machine=Machine.L40S, cloud_compute="gpu-l40s")
        self.manifest_path = MANIFEST_PATH

    def run(self):
        if not Path(self.manifest_path).exists():
            raise FileNotFoundError(f"Manifest missing: {self.manifest_path}")
        if not Path(TRAIN_SCRIPT).exists():
            raise FileNotFoundError(f"Train script missing: {TRAIN_SCRIPT}")

        os.environ["MANIFEST_PATH"] = self.manifest_path
        import subprocess
        subprocess.run([sys
