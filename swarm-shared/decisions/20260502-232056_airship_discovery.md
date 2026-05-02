# airship / discovery

## Final Unified Implementation Plan  
*(Best parts from both proposals, contradictions resolved in favor of correctness + concrete actionability)*

---

### Goal (unchanged, tightened)
Harden `airship discover` into a **deterministic, CDN-only orchestrator** that:
- Eliminates HF API rate limits (429) by using **one-time tree list + CDN fetches only**.
- Eliminates PyArrow `CastError` / schema mismatches by **projecting to `{prompt, response}` at parse time and dropping all other fields**.
- Produces **reproducible artifacts** (`file-list.json`, `manifest.json`, and optional merged parquet) usable by downstream surrogate-1 training.
- Ships in ≤2h: no training/infra changes; single orchestration script.

---

### Resolved Contradictions
1. **Tree listing scope**  
   - Candidate 1: non-recursive per date folder (correct).  
   - Candidate 2: implied recursive/full repo (risky, reintroduces scale/rate issues).  
   → **Adopt Candidate 1**: one non-recursive `list_repo_tree` per date folder only.

2. **Projection safety**  
   - Candidate 1: robust per-type projection with fallback for parquet.  
   - Candidate 2: incomplete details.  
   → **Adopt Candidate 1** with small hardening: strict allow-list columns and explicit error handling.

3. **CDN fallback vs primary strategy**  
   - Candidate 1: tries `hf_hub_download` first, then CDN GET.  
   - Candidate 2: implies direct CDN use but lacks code.  
   → **Adopt Candidate 1 pattern** but prefer **direct CDN GET** (no auth, no rate limit) as primary; keep `hf_hub_download` only if CDN fails (rare). This is simpler and eliminates 429 risk entirely.

4. **CLI arguments**  
   - Candidate 1: clear (`--repo`, `--date`, `--out`, `--skip-project`).  
   - Candidate 2: missing.  
   → **Adopt Candidate 1** exactly.

5. **Idempotency & logging**  
   - Both mention it; Candidate 1 has concrete `manifest.json` with sha256.  
   → **Adopt Candidate 1** exactly.

---

### Implementation Plan (≤2h)

1. **Locate entrypoint**  
   Find `airship discover` CLI (likely `/opt/axentx/airship/discover.py` or similar). Replace or update it with the unified script below.

2. **CDN-only deterministic listing**  
   - One non-recursive `list_repo_tree` per date folder.  
   - Save sorted `file-list.json` + `manifest.json` with sha256 of canonical file list.

3. **Schema hardening during ingestion**  
   - Download each file via direct CDN GET (`https://huggingface.co/datasets/<repo>/resolve/main/<date>/<file>`).  
   - Parse and **project to `{prompt, response}` only**; drop all other fields.  
   - Support JSONL and Parquet; reject unsupported types with clear error.

4. **Idempotent outputs**  
   - Skip already-downloaded files by slug (filename-based).  
   - Write `file-list.json`, `manifest.json`, and optional merged parquet to `batches/mirror-merged/{date}/`.

5. **Validation & smoke test**  
   - Run:  
     ```bash
     airship discover --repo datasets/airship-mirror --date 2026-04-29 --out ./artifacts
     ```
   - Verify: no 429, no CastError, `file-list.json` produced, downstream surrogate-1 loader can consume parquet.

---

### Final Code: `airship/discover.py`

```python
#!/usr/bin/env python3
"""
airship discover - CDN-only deterministic file lister and projector.
Eliminates HF API rate limits and mixed-schema CastErrors.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests

SUPPORTED_REPOS = [
    "datasets/airship-mirror",
    "datasets/surrogate-1-ingest",
]

def deterministic_file_list(repo: str, date_folder: str) -> List[str]:
    """
    One API call: non-recursive tree per date folder.
    Returns sorted relative paths (deterministic).
    """
    try:
        from huggingface_hub import list_repo_tree
        tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    except Exception as exc:
        raise RuntimeError(f"Failed to list {repo}/{date_folder}: {exc}") from exc

    files = sorted([item.rfilename for item in tree if item.type == "file"])
    return files

def build_manifest(repo: str, date_folder: str, files: List[str]) -> Dict[str, Any]:
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "file_count": len(files),
        "files": files,
    }
    list_bytes = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    manifest["sha256"] = hashlib.sha256(list_bytes).hexdigest()
    return manifest

def save_manifest(manifest: Dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    list_path = out_dir / "file-list.json"
    manifest_path = out_dir / "manifest.json"
    list_path.write_text(json.dumps(manifest, indent=2))
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} files -> {list_path}")
    print(f"Manifest: {manifest_path}")
    return list_path

def cdn_download(repo: str, date_folder: str, rel_path: str, dest: Path) -> Path:
    cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{rel_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(cdn_url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest

def project_to_prompt_response(file_path: Path) -> List[Dict[str, str]]:
    suffix = file_path.suffix.lower()

    if suffix == ".jsonl":
        rows = []
        for line in file_path.read_text().strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            rows.append({
                "prompt": str(obj.get("prompt", obj.get("input", ""))),
                "response": str(obj.get("response", obj.get("output", ""))),
            })
        return rows

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            tbl = pq.read_table(file_path)
        except Exception as exc:
            raise ValueError(f"Failed to read parquet {file_path}: {exc}") from exc

        allowed = {"prompt", "response"}
        missing = allowed - set(tbl.column_names)
        if missing:
            raise ValueError(f"Parquet missing required columns {missing} in {file_path}")

        tbl = tbl.select(["prompt", "response"])
        df = tbl.to_pandas()
        return df[["prompt", "response"]].to_dict(orient="records")

    raise ValueError(f"Unsupported file type: {suffix}")

def download_and_project(
    repo: str,
    date_folder: str,
    file_list: List[str],
    out_dir: Path,
) -> Path:
    merged_dir = out_dir / "batches" / "mirror-merged" / date_folder
    merged_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for rel_path in file_list:
        slug = Path(rel_path).stem
        dest = out_dir / "downloads" / date_folder.replace("/", "_") / f"{slug}{Path(rel_path).suffix}"
        if dest.exists():
            print(f"Skipping existing {rel_path}")
        else:
            print(f"Downloading {rel_path}...")
            cdn_download(repo, date_folder, rel_path, dest)

        try:
            rows = project_to_prompt_response(dest)
            all_rows.extend(rows)
        except Exception
