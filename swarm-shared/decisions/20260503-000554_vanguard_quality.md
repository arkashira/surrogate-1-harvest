# vanguard / quality

## 1. Diagnosis
- No persisted manifest per `(repo, dateFolder)` → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Training uses `load_dataset(streaming=True)` on heterogeneous repos → `pyarrow.CastError` on mixed schemas.
- No pre-flight validation in UI → users select invalid/malformed paths and fail late.
- Lightning Studio recreation on every run → quota waste (~80hr/mo) and cold-start delays.
- No CDN-only fallback path → API rate limits block data loading during training.

## 2. Proposed change
Create `/opt/axentx/vanguard/manifest.py` + `/opt/axentx/vanguard/train.py` patch:
- `manifest.py`: single function `build_manifest(repo, date_folder)` that calls HF API **once** (or reads cached JSON), returns list of CDN URLs; caches to `.cache/vanguard/manifests/{repo}/{date_folder}.json`.
- `train.py`: replace `load_dataset(streaming=True)` with CDN-only `FileDataset` that reads from the manifest; project to `{prompt, response}` at parse time.
- Add lightweight schema validation and UI pre-flight helper.

## 3. Implementation

```bash
# /opt/axentx/vanguard/manifest.py
import json, os, time, hashlib
from pathlib import Path
from huggingface_hub import list_repo_tree, hf_hub_download

CACHE_ROOT = Path(".cache/vanguard/manifests")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

def _cache_path(repo: str, date_folder: str) -> Path:
    slug = repo.replace("/", "--")
    return CACHE_ROOT / slug / f"{date_folder}.json"

def build_manifest(repo: str, date_folder: str, bust: bool = False):
    """
    Returns list of dict:
      {"cdn_url": "...", "local_path": "...", "size": int}
    Uses HF API once per (repo,date_folder), then CDN-only thereafter.
    """
    cp = _cache_path(repo, date_folder)
    if not bust and cp.exists():
        return json.loads(cp.read_text())

    # Single API call: non-recursive tree for this folder only
    items = list_repo_tree(repo, path=date_folder, recursive=False)
    files = [i for i in items if i.type == "file"]

    manifest = []
    for f in files:
        cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
        local = hf_hub_download(repo_id=repo, filename=f.path, repo_type="dataset")
        manifest.append({
            "cdn_url": cdn,
            "local_path": local,
            "size": f.size,
            "filename": os.path.basename(f.path)
        })

    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(manifest, indent=2))
    return manifest

def validate_pair(manifest):
    """Light schema check: keep only files that look like prompt/response pairs."""
    ok = []
    for m in manifest:
        name = m["filename"].lower()
        # crude but effective for surrogate-1 convention
        if any(k in name for k in ("pair", "conv", "chat", "qa", "instruct")):
            ok.append(m)
    return ok
```

```python
# /opt/axentx/vanguard/train.py  (minimal diff)
# Replace:
#   from datasets import load_dataset
#   ds = load_dataset("repo", streaming=True, split="train")
# With:
from manifest import build_manifest, validate_pair
from datasets import Dataset
import pyarrow as pa

def load_cdn_only(repo, date_folder):
    manifest = validate_pair(build_manifest(repo, date_folder))
    # Read only local cached files; project to {prompt,response}
    pairs = []
    for m in manifest:
        try:
            # surrogate-1 convention: each file is line-delimited JSON with prompt/response
            import json as _json
            with open(m["local_path"]) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = _json.loads(line)
                    # robust projection
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                    response = obj.get("response") or obj.get("output") or obj.get("answer")
                    if prompt is None or response is None:
                        continue
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        except Exception as e:
            print(f"Skip {m['filename']}: {e}")
            continue
    return Dataset.from_pylist(pairs)

# usage
# ds = load_cdn_only("my-org/surrogate-1", "2026-04-29")
```

```python
# /opt/axentx/vanguard/ui_preflight.py  (optional helper)
from manifest import build_manifest, validate_pair

def preflight(repo, date_folder):
    manifest = build_manifest(repo, date_folder)
    valid = validate_pair(manifest)
    return {
        "total_files": len(manifest),
        "valid_pairs": len(valid),
        "cdn_urls": [v["cdn_url"] for v in valid[:10]],
        "estimated_tokens": sum(v["size"] for v in valid) // 4  # rough
    }
```

## 4. Verification
1. Run once on a small repo/date:
   ```bash
   cd /opt/axentx/vanguard
   python -c "from manifest import build_manifest; m=build_manifest('databricks/databricks-dolly-15k','main'); print(len(m))"
   ```
   Expect: non-zero count and `.cache/vanguard/manifests/databricks--databricks-dolly-15k/main.json` created.

2. Confirm CDN-only behavior:
   - Disconnect network or revoke HF token.
   - Re-run same command; it should load instantly from cache (no API calls).

3. Confirm schema resilience:
   - Place a malformed JSON line in one cached file; `train.py` should skip it and continue.

4. Confirm Lightning reuse:
   - In your launcher script, add:
     ```python
     from lightning import Teamspace
     running = [s for s in Teamspace.studios() if s.name == "vanguard-train" and s.status == "running"]
     studio = running[0] if running else Studio.create(name="vanguard-train", machine="L40S")
     ```
   - Verify no duplicate studios are created on repeated runs.
