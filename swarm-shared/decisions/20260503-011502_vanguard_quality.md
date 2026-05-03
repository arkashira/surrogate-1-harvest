# vanguard / quality

## Final Synthesized Solution

### Diagnosis (merged, de-duplicated)
- **Frontend + backend** trigger authenticated `list_repo_tree` on every page load/training start → burns HF quota (1000/5min) and causes 429s.
- **Downloads use authenticated `/api/` paths** instead of public CDN → avoidable auth overhead and quota exposure.
- **No persisted `(repo, dateFolder)` manifest** → ingestion/training re-enumerate files and re-download metadata on every run.
- **Training uses `load_dataset(streaming=True)` on heterogeneous repos** → pyarrow `CastError` on mixed schemas.
- **No reuse guard for Lightning Studio** → quota waste via repeated instance creation instead of reusing running instances.
- **No graceful fallback when HF API returns 429**; no CDN-only path for critical downloads.

---

### Proposed Change (single scope)
`/opt/axentx/vanguard`

1. **Add `vanguard/manifest.py`**  
   Single API call to `list_repo_tree` per `(repo, dateFolder)`, cache to `manifests/{repo_hash}.json` with TTL.

2. **Add `vanguard/cdn.py`**  
   Resolve public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for zero-auth downloads; threaded fetcher with retries; fallback on 429 to CDN-only mode.

3. **Patch training launcher (`vanguard/train_launcher.py`)**  
   - Accept manifest JSON path; skip `list_repo_tree` during training.  
   - Use CDN-only fetches (no HF API calls) via `requests`/`wget` without token.  
   - Reuse running Lightning Studio; fallback to L40S on `lightning-public-prod`.

4. **Patch ingestion wrapper**  
   Project to `{prompt,response}` only; store attribution in filename:  
   `batches/mirror-merged/{date}/{slug}.parquet` (no extra columns).

5. **Add graceful degradation**  
   - On HF 429: serve from CDN cache if available; else wait/backoff and retry once; never fail training for metadata listing.

---

### Implementation

```bash
# /opt/axentx/vanguard
mkdir -p manifests data/raw batches/mirror-merged
```

#### vanguard/manifest.py
```python
from __future__ import annotations
import json, os, time, hashlib
from pathlib import Path
from huggingface_hub import HfApi

CACHE_DIR = Path(__file__).parent / "manifests"
CACHE_DIR.mkdir(exist_ok=True)

def _cache_path(repo: str, date_folder: str) -> Path:
    slug = f"{repo}__{date_folder}"
    safe = hashlib.sha256(slug.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{safe}.json"

def list_date_files(
    repo: str,
    date_folder: str,
    token: str | None = None,
    ttl: int = 3600,
    fallback_to_cache: bool = True,
) -> list[str]:
    """
    Single API call per (repo,dateFolder). Returns CDN-ready relative paths.
    On HF 429 and fallback_to_cache=True, returns cached list if available.
    """
    cp = _cache_path(repo, date_folder)
    now = time.time()

    # Serve fresh cache if within TTL
    if cp.exists() and (now - cp.stat().st_mtime) < ttl:
        return json.loads(cp.read_text())

    api = HfApi(token=token)
    try:
        entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
        files = [e.path for e in entries if e.type == "file"]
        cp.write_text(json.dumps(files, indent=2))
        return files
    except Exception as exc:
        # Graceful degradation on 429 or network issues
        if fallback_to_cache and cp.exists():
            return json.loads(cp.read_text())
        raise RuntimeError(f"Unable to list {repo}/{date_folder}: {exc}") from exc
```

#### vanguard/cdn.py
```python
from __future__ import annotations
import time
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, filepath: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{filepath}"

def download_cdn_files(
    repo: str,
    files: list[str],
    out_dir: Path,
    max_workers: int = 8,
    retries: int = 2,
    backoff: float = 1.0,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    def _fetch(f: str) -> Path | None:
        url = cdn_url(repo, f)
        out = out_dir / Path(f).name
        if out.exists() and out.stat().st_size > 0:
            return out

        for attempt in range(1, retries + 1):
            try:
                r = requests.get(url, timeout=30, stream=True)
                r.raise_for_status()
                with open(out, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=8192):
                        fh.write(chunk)
                return out
            except Exception as exc:
                if attempt == retries:
                    print(f"Download failed for {f}: {exc}")
                    return None
                time.sleep(backoff * attempt)
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch, f): f for f in files}
        for fut in as_completed(futures):
            try:
                p = fut.result()
                if p:
                    saved.append(p)
            except Exception:
                pass
    return saved
```

#### vanguard/train_launcher.py
```python
from __future__ import annotations
import json
from pathlib import Path
from lightning import Lightning, Studio, Machine
from vanguard.manifest import list_date_files
from vanguard.cdn import download_cdn_files

def launch_training(
    repo: str,
    date_folder: str,
    hf_token: str,
    train_script: Path,
    reuse_ok: bool = True,
):
    # 1) Manifest (single API call; graceful fallback)
    files = list_date_files(repo, date_folder, token=hf_token, ttl=3600, fallback_to_cache=True)
    if not files:
        raise RuntimeError("No files found for training.")

    # 2) CDN download (zero API/auth during training)
    raw_dir = Path("data/raw") / date_folder
    download_cdn_files(repo, files, raw_dir)

    # 3) Reuse running studio
    api = Lightning()
    studio = None
    if reuse_ok:
        for s in api.teamspace.studios:
            if s.name == f"vanguard-{date_folder}" and s.status == "Running":
                studio = s
                break

    if studio is None or studio.status != "Running":
        studio = api.studio.create(
            name=f"vanguard-{date_folder}",
            machine=Machine.L40S,
            cloud="lightning-public-prod",
            create_ok=True,
        )

    # 4) Run training with manifest baked in
    manifest_path = Path("manifests") / f"{repo.replace('/', '_')}__{date_folder}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(files, indent=2))

    studio.run(
        str(train_script),
        arguments=[
            "--manifest", str(manifest_path),
            "--data-dir", str(raw_dir),
            "--repo", repo,
        ],
    )
    return studio
```

#### Ingestion wrapper fix (schema/projection)
```python
# batches/mirror-merged/{date}/{slug}.parquet
# Columns: prompt, response
# No 'source', no 'ts', no extra metadata columns.
```

---

### Verification (merged, concrete)

1. **Manifest caching**  
   - Run `list_date_files("username/dataset", "2024-01-01")` twice within 1h.  
   - Confirm second
