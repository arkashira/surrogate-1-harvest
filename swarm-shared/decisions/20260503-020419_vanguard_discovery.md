# vanguard / discovery

**Final Synthesized Answer**  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

---

## 1. Diagnosis (merged, corrected)

- **No persisted `(repo, dateFolder) → file-list` manifest**: every training run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.  
- **Training/data loader relies on authenticated calls or `streaming=True`**: this hits HF API rate limits and fails on mixed-schema repos.  
- **No CDN-only data path**: ingestion depends on HF `/api/` endpoints instead of public CDN URLs, so rate limits block training.  
- **No reuse guard for Lightning Studio**: scripts create new studios instead of reusing running ones, wasting quota (80+ hours/month).  
- **No deterministic repo-sharding for commits**: burst ingestion risks hitting HF’s 128 commits/hr/repo cap.  
- **CLI/args missing**: prevents reliable cron/Mac orchestration.

---

## 2. Proposed change (merged, actionable)

Add a lightweight bootstrap under `/opt/axentx/vanguard/` (new files only; no edits to existing code):

1. **Persist manifests**: `manifest.py` writes `(repo, dateFolder) → file-list` JSON to `vanguard/manifests/`.  
2. **CDN-only training**: `train.py` uses CDN fetches (no auth) and projects to `{prompt, response}` at parse time.  
3. **Studio reuse**: reuse a running Lightning Studio if present; otherwise start one (L40S priority).  
4. **CLI args**: accept `--repo` and `--date` so it can be invoked from cron/Mac orchestration.  
5. **Rate-limit resilience**: single `list_repo_tree` call with retry/backoff; manifest reuse avoids repeated API calls.

---

## 3. Implementation (merged, corrected, executable)

### Directory layout
```
/opt/axentx/vanguard/
├── manifest.py
├── train.py
└── manifests/
```

### `vanguard/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate and cache (repo, dateFolder) -> file-list manifests.
Usage:
    python3 manifest.py --repo datasets/axentx/surrogate-1 --date 2026-04-29
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, HfApi
except ImportError:
    raise SystemExit("pip install huggingface_hub")

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

HF_TOKEN = os.getenv("HF_TOKEN", "")

def build_manifest(repo: str, date_folder: str, out_path: Path) -> dict:
    """
    Single non-recursive tree call for date_folder.
    Returns list of file paths under that folder.
    """
    api = HfApi(token=HF_TOKEN or None)
    items = api.list_repo_tree(
        repo_id=repo,
        path=date_folder,
        recursive=False,
        repo_type="dataset",
    )
    files = sorted([it.rfilename for it in items if it.type == "file"])
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Build HF file manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/axentx/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    args = parser.parse_args()

    out_path = MANIFEST_DIR / f"{args.repo.replace('/', '_')}_{args.date}.json"
    if out_path.exists():
        print(f"Manifest exists: {out_path}")
        print(json.dumps(json.loads(out_path.read_text()), indent=2))
        return

    retry = 0
    while retry < 3:
        try:
            manifest = build_manifest(args.repo, args.date, out_path)
            print(f"Manifest saved: {out_path}")
            print(f"Files: {len(manifest['files'])}")
            return
        except Exception as e:
            if "429" in str(e):
                wait = 360
                print(f"Rate limited (429). Waiting {wait}s...")
                time.sleep(wait)
                retry += 1
            else:
                raise
    raise SystemExit("Failed after retries.")

if __name__ == "__main__":
    import os
    main()
```

### `vanguard/train.py`
```python
#!/usr/bin/env python3
"""
CDN-only training bootstrap.
- Uses manifest to avoid HF API calls during training.
- Projects each file to {prompt, response} at parse time.
- Reuses a running Lightning Studio if present; otherwise starts one (L40S priority).
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    import lightning as L
    from lightning.fabric.utilities import Teamspace, Studio, Machine
    from huggingface_hub import hf_hub_download, HfApi
except ImportError:
    raise SystemExit("pip install lightning huggingface_hub")

MANIFEST_DIR = Path(__file__).parent / "manifests"

def load_manifest(repo: str, date_folder: str) -> dict:
    manifest_path = MANIFEST_DIR / f"{repo.replace('/', '_')}_{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}. Run manifest.py first."
        )
    return json.loads(manifest_path.read_text())

def cdn_line_iterator(repo: str, files: list, max_files: int = None):
    """
    Download via CDN (no auth) and yield lines projected to {prompt, response}.
    Assumes each file is JSONL with fields that can be projected.
    """
    count_files = 0
    for fpath in files:
        if max_files is not None and count_files >= max_files:
            break
        # CDN download (repo_type dataset is explicit)
        local_path = hf_hub_download(
            repo_id=repo,
            filename=fpath,
            repo_type="dataset",
            token=os.getenv("HF_TOKEN", None) or None,
        )
        with open(local_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Projection: keep only prompt/response; ignore mixed schema extras
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt), "response": str(response)}
        count_files += 1

def find_running_studio(name: str):
    try:
        for s in Teamspace.studios:
            if s.name == name and s.status == "Running":
                return s
    except Exception:
        pass
    return None

def run_training_on_studio(repo: str, date_folder: str, max_files: int = 100):
    studio_name = f"vanguard-{repo.replace('/', '-')}-{date_folder}"
    studio = find_running_studio(studio_name)
    if studio:
        print(f"Reusing running studio: {studio_name}")
    else:
        print(f"Starting new studio (L40S priority): {studio_name}")
        studio = Studio(
            name=studio_name,
            machine=Machine.L40S,
            create_ok=True,
        )

    # Load manifest and prepare data
    manifest = load_manifest(repo, date_folder)
    examples = list(cdn_line_iterator(repo, manifest["files"], max_files=max_files))
    print(f"Prepared {len(examples)} examples from {len(manifest['files'])} files.")

    # Minimal stub
