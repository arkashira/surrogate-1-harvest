# airship / discovery

## Final Consolidated Plan (Highest-Value, <2h)

**Ship:** Deterministic CDN file manifest generator + Lightning Studio lifecycle resilience for Surrogate-1 training.

**Why (merged):**  
- Eliminates HF API 429s during Surrogate training by pre-listing one date folder and using CDN-only fetches.  
- Prevents idle-stop training loss by checking studio status before each run and auto-restarting if stopped.  
- Fits within 2h: small generator script + training script guard + optional reuse helper.

---

## Implementation Plan (merged, corrected, actionable)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | me | 15m | Add `tools/build_cdn_manifest.py` — takes `repo`, `date_folder`, outputs `manifest.json` with `path`, `cdn_url`, `sha256` (optional). Uses single `list_repo_tree` call, then writes CDN URLs. |
| 2 | me | 20m | Add `surrogate/training/cdn_dataset.py` — reads `manifest.json`, uses `requests` to stream from CDN URLs directly (no HF datasets). Wrap dataset in `IterableDataset` that yields `{prompt, response}`. |
| 3 | me | 15m | Add `surrogate/training/studio_lifecycle.py` — before `.run()`, list running studios; reuse if exists and running; if stopped, restart with `target.start(machine=Machine.L40S)`. **Fix:** complete the truncated wait loop and add robust refresh/timeout. |
| 4 | me | 30m | Update `surrogate/training/launch.py` (or equivalent) to call manifest build once (Mac orchestration), then submit Lightning job with manifest baked in. |
| 5 | me | 20m | Smoke test locally (small manifest, 5–10 files) and verify Lightning Studio can start/resume and train without HF API calls. |
| 6 | me | 20m | Add cron-safe shebang + executable bits to any wrapper scripts (if present) and set `SHELL=/bin/bash` in crontab comments. |

Total: ~2h.

---

## Code Snippets (merged + corrected)

### 1) tools/build_cdn_manifest.py
```python
#!/usr/bin/env python3
"""
Build a deterministic CDN manifest for one date folder in a HuggingFace dataset repo.
Usage:
  python tools/build_cdn_manifest.py \
    --repo datasets/your-org/surrogate-mirror \
    --date 2026-04-29 \
    --out manifest.json
"""
import argparse
import json
import sys
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder(repo: str, date_folder: str) -> List[str]:
    api = HfApi()
    # Non-recursive per folder to avoid pagination explosion; we only want one date folder.
    folder_path = date_folder.rstrip("/")
    items = api.list_repo_tree(repo=repo, path=folder_path, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    return files

def build_manifest(repo: str, date_folder: str) -> Dict:
    files = list_date_folder(repo, date_folder)
    entries = []
    for f in sorted(files):
        path = f"{date_folder}/{f}"
        entry = {
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
        }
        entries.append(entry)
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "tools/build_cdn_manifest.py",
        "entries": entries,
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN manifest for HF dataset date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/your-org/name)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()

    print(f"Listing {args.repo}/{args.date} ...")
    manifest = build_manifest(args.repo, args.date)
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(manifest['entries'])} entries to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x tools/build_cdn_manifest.py
```

---

### 2) surrogate/training/cdn_dataset.py
```python
import json
import io
from typing import Iterator, Dict, Any
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """
    Stream {prompt, response} from parquet files listed in manifest.json using CDN URLs.
    Projects only {prompt, response} at parse time to avoid mixed-schema issues.
    """
    def __init__(self, manifest_path: str, start_idx: int = 0, end_idx: int = -1):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.entries = manifest["entries"]
        if end_idx < 0:
            end_idx = len(self.entries)
        self.entries = self.entries[start_idx:end_idx]

    def _stream_parquet(self, cdn_url: str) -> Iterator[Dict[str, Any]]:
        # Stream download and parse with pyarrow (no HF datasets)
        import pyarrow.parquet as pq
        resp = requests.get(cdn_url, stream=True, timeout=60)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        table = pq.read_table(buf)
        # Project only prompt/response; ignore other columns
        cols = set(table.column_names)
        has_prompt = "prompt" in cols
        has_response = "response" in cols
        if not (has_prompt and has_response):
            # Best-effort fallback: look for common aliases
            pass
        df = table.select_columns(["prompt", "response"]).to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": str(row["prompt"]), "response": str(row["response"])}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for entry in self.entries:
            try:
                yield from self._stream_parquet(entry["cdn_url"])
            except Exception as exc:
                # Log and skip corrupt files; don't crash training
                print(f"Skipping {entry['path']}: {exc}")
                continue
```

---

### 3) surrogate/training/studio_lifecycle.py
```python
#!/usr/bin/env python3
"""
Lightning Studio lifecycle helpers: reuse running studios, restart stopped ones.
"""
import time
from lightning import Lightning, Teamspace, Studio, Machine, L40S

def get_or_create_studio(name: str, target_machine: Machine = L40S) -> Studio:
    teamspace = Teamspace()
    running = [s for s in teamspace.studios if s.name == name and s.status == "Running"]
    if running:
        print(f"Reusing running studio: {name}")
        return running[0]

    stopped = [s for s in teamspace.studios if s.name == name and s.status == "Stopped"]
    if stopped:
        studio = stopped[0]
        print(f"Restarting stopped studio: {name}")
        studio.start(machine=target_machine)
        # Wait until running
        while studio.status != "Running":
            time.sleep(10)
            studio.refresh()
        return studio

    print(f"Creating new studio: {name}")
    return Studio.create(name=name, machine=target_machine, create_ok=True)

def ensure_running(studio: Studio, timeout_seconds: int = 300) -> bool:
    studio.refresh()
    if studio.status == "Running":
        return True

    print(f"Studio {studio.name} is {studio.status}. Restarting...")
    studio.start(machine=L40S)

    # Wait with
