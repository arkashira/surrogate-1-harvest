# vanguard / quality

## 1. Diagnosis
- No content-addressed manifest → training/UI hit HF API at runtime (429s, non-reproducible epochs, no shareable snapshots).
- Dataset ingestion writes mixed-schema files to `enriched/` (extra `source`, `ts` cols) instead of projecting to `{prompt, response}` only.
- No CDN bypass strategy → ingestion/training consume API quota instead of using public CDN URLs.
- No pre-listed file manifest → each epoch re-enumerates repo via API (rate-limit + non-deterministic ordering).
- No deterministic repo selection for commit-cap mitigation → all writes target one repo and risk 128/hr cap.

## 2. Proposed change
Create `/opt/axentx/vanguard/ingest/manifest.py` and update ingestion entrypoint to:
- Produce a content-addressed manifest (sha256 per file) saved as `batches/mirror-merged/{date}/manifest.json`.
- Project to `{prompt, response}` only and drop extra columns.
- Embed CDN URLs (no auth) and deterministic repo selection via hash-slug → sibling repo.
- Export a training-ready file list so Lightning jobs use CDN-only fetches (zero API calls during training).

## 3. Implementation

```bash
# /opt/axentx/vanguard/ingest/manifest.py
#!/usr/bin/env python3
"""
Content-addressed manifest generator for HF dataset ingestion.
- Projects to {prompt, response}
- Emits CDN URLs (bypasses API rate limits)
- Deterministic repo selection to avoid 128/hr commit cap
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/vanguard-mirror")
HF_CDN_ROOT = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
SIBLING_REPOS = [
    "datasets/axentx/vanguard-mirror",
    "datasets/axentx/vanguard-mirror-s1",
    "datasets/axentx/vanguard-mirror-s2",
    "datasets/axentx/vanguard-mirror-s3",
    "datasets/axentx/vanguard-mirror-s4",
]


def _hash_slug(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def pick_sibling_repo(slug: str) -> str:
    """Deterministic repo selection from sibling pool."""
    h = int(_hash_slug(slug), 16)
    return SIBLING_REPOS[h % len(SIBLING_REPOS)]


def project_record(raw: Dict) -> Dict:
    """
    Project raw record to canonical {prompt, response}.
    Accepts common key variants and normalizes.
    """
    prompt = (
        raw.get("prompt")
        or raw.get("input")
        or raw.get("question")
        or raw.get("instruction")
        or ""
    )
    response = (
        raw.get("response")
        or raw.get("output")
        or raw.get("answer")
        or raw.get("completion")
        or ""
    )
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}


def build_manifest(
    date_str: str,
    file_paths: List[str],
    records_by_file: Dict[str, List[Dict]],
) -> Dict:
    """
    Build content-addressed manifest for a date folder.
    file_paths: relative paths within HF dataset repo (e.g. "2026-05-03/file.jsonl")
    records_by_file: parsed records per file (already projected)
    """
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_repo": HF_DATASET_REPO,
        "date": date_str,
        "files": [],
    }

    for rel_path in file_paths:
        records = records_by_file.get(rel_path, [])
        if not records:
            continue

        # content-address file by canonical JSON lines
        canonical = "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records)
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        slug = f"{date_str}/{Path(rel_path).stem}"
        target_repo = pick_sibling_repo(slug)

        # CDN URL for direct download (no auth)
        cdn_url = f"{HF_CDN_ROOT}/{rel_path}"

        manifest["files"].append(
            {
                "rel_path": rel_path,
                "slug": slug,
                "content_hash": content_hash,
                "cdn_url": cdn_url,
                "target_repo": target_repo,
                "record_count": len(records),
                "schema": ["prompt", "response"],
            }
        )

    manifest["total_records"] = sum(f["record_count"] for f in manifest["files"])
    return manifest


def save_manifest(manifest: Dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = manifest["date"]
    out_path = output_dir / f"manifest.json"
    # Also keep dated copy for reproducibility
    dated_path = output_dir / f"manifest-{date_str}.json"
    for p in (out_path, dated_path):
        p.write_text(json.dumps(manifest, indent=2))
    return out_path


if __name__ == "__main__":
    # Minimal CLI for testing: expects date and newline-delimited JSON files in ./raw/
    import sys
    from glob import glob

    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    raw_dir = Path("./raw")
    output_dir = Path(f"batches/mirror-merged/{date_str}")

    file_paths = []
    records_by_file = {}
    for fp in raw_dir.rglob("*.jsonl"):
        rel = str(fp.relative_to(raw_dir))
        records = [project_record(json.loads(ln)) for ln in fp.read_text().splitlines() if ln.strip()]
        file_paths.append(rel)
        records_by_file[rel] = records

    manifest = build_manifest(date_str, file_paths, records_by_file)
    saved = save_manifest(manifest, output_dir)
    print(f"Manifest saved: {saved}")
```

Update ingestion script (example snippet to integrate):
```python
# In your existing ingestion entrypoint (e.g. ingest/run.py)
from vanguard.ingest.manifest import build_manifest, save_manifest

# After downloading & parsing files into `records_by_file` keyed by rel_path:
manifest = build_manifest(date_str, file_paths, records_by_file)
manifest_path = save_manifest(manifest, Path(f"batches/mirror-merged/{date_str}"))

# Emit training file list for Lightning (CDN-only)
file_list = [f["cdn_url"] for f in manifest["files"]]
(Path("batches/mirror-merged") / date_str / "filelist-cdn.txt").write_text("\n".join(file_list))
```

Training script usage (Lightning):
```python
# train.py — embed file list, CDN-only data loader
import json
from pathlib import Path
import requests
from torch.utils.data import IterableDataset

class CDNJsonlDataset(IterableDataset):
    def __init__(self, filelist_path):
        self.urls = [ln.strip() for ln in Path(filelist_path).read_text().splitlines() if ln.strip()]

    def __iter__(self):
        for url in self.urls:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            for line in resp.text.splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                # accept canonical {prompt,response} or project
                yield {"prompt": obj.get("prompt", ""), "response": obj.get("response", "")}
```

## 4. Verification
1. Run manifest generation on a small sample:
   ```bash
   cd /opt/axentx/vanguard
   python -m ingest.manifest 2026-05-03
   ```
   Confirm `batches/mirror-merged/2026-05-03/manifest.json` exists, contains `cdn_url` fields, `schema: ["prompt","response"]`, and no `source`/`ts` columns in projected
