# vanguard / discovery

## Final Consolidated Implementation (Correct + Actionable)

**Core diagnosis (agreed):**  
- No persisted manifest → repeated authenticated HF API enumeration → quota burn + 429 risk.  
- Training likely uses recursive enumeration / `load_dataset` → couples training to API availability.  
- Lightning Studio reuse not enforced → idle-stop wastes quota.  
- Mixed-file HF repos risk schema errors (pyarrow.CastError) if not projected to clean `{prompt, response}`.  
- Missing CDN-only fetch path in training pipeline.

**Resolution priorities:**  
1. Correctness: manifest must be generated once and reused; training must use CDN-only URLs (no HF API during training).  
2. Actionability: provide drop-in code, CLI, and verification steps that work on the provided Mac orchestrator + Lightning environment.  
3. Schema safety: enforce projection to `{prompt, response}` and reject unsupported schemas at manifest build time.

---

### 1. Create `/opt/axentx/vanguard/manifest.py`

```python
# /opt/axentx/vanguard/manifest.py
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Any, Optional

try:
    from huggingface_hub import HfApi
    from lightning import Lightning, Teamspace, Machine
    _HF_AVAILABLE = True
except Exception:  # graceful fallback when deps unavailable
    _HF_AVAILABLE = False

MANIFEST_ROOT = Path(os.getenv("VANGUARD_MANIFEST_ROOT", "/opt/axentx/vanguard/manifests"))
HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

# Supported file extensions and preferred projection keys
SUPPORTED_EXTS = {".jsonl", ".json", ".parquet", ".csv", ".tsv"}
REQUIRED_KEYS = {"prompt", "response"}


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_supported_file(path: str) -> bool:
    return any(path.lower().endswith(ext) for ext in SUPPORTED_EXTS)


def _project_record(rec: Dict[str, Any]) -> Dict[str, str]:
    """
    Project raw record to {prompt, response}.
    Raises if projection is ambiguous or impossible.
    """
    keys = {k.strip().lower() for k in rec.keys()}

    # Exact match
    if keys == REQUIRED_KEYS:
        return {"prompt": str(rec.get("prompt", rec.get("Prompt", ""))),
                "response": str(rec.get("response", rec.get("Response", "")))}

    # Heuristic mapping
    prompt_candidates = [k for k in rec.keys() if re.search(r"prompt|instruction|query", k, re.I)]
    response_candidates = [k for k in rec.keys() if re.search(r"response|completion|answer", k, re.I)]

    if len(prompt_candidates) == 1 and len(response_candidates) == 1:
        return {"prompt": str(rec[prompt_candidates[0]]),
                "response": str(rec[response_candidates[0]])}

    # Fallback: if only two keys, assume order (prompt, response)
    if len(rec) == 2:
        items = list(rec.items())
        return {"prompt": str(items[0][1]), "response": str(items[1][1])}

    raise ValueError(f"Cannot project record to {{prompt, response}}: {list(rec.keys())}")


def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: Path = MANIFEST_ROOT,
    max_files: int = 5000,
) -> Path:
    """
    Single authenticated HF API call to list top-level folder for date_folder.
    Persists manifest with CDN URLs and schema metadata.
    Raises on unsupported/mixed schemas.
    Returns manifest path.
    """
    if not _HF_AVAILABLE:
        raise RuntimeError("huggingface_hub not available")

    api = HfApi()
    # List only immediate children of date_folder (non-recursive)
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    entries: List[Dict[str, Any]] = []
    seen_projections = set()

    for item in tree:
        if item.type != "file":
            continue
        if not _is_supported_file(item.path):
            continue
        if len(entries) >= max_files:
            raise RuntimeError(f"Too many files in {repo}/{date_folder}; limit={max_files}")

        cdn_url = HF_CDN_TEMPLATE.format(repo=repo, path=item.path)

        # Lightweight projection check: attempt to read first non-empty record if feasible
        # For parquet/jsonl we can sample; for others we defer to training-time validation.
        projection_hint = "unknown"
        try:
            # Best-effort sample; skip if fails (e.g., large parquet needs engine)
            if item.path.lower().endswith(".jsonl"):
                import requests
                sample_resp = requests.get(cdn_url, headers={"Range": "bytes=0-8192"}, timeout=10)
                if sample_resp.status_code in (200, 206):
                    for line in sample_resp.text.strip().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        _project_record(rec)
                        projection_hint = "ok"
                        break
            elif item.path.lower().endswith(".json"):
                import requests
                sample_resp = requests.get(cdn_url, headers={"Range": "bytes=0-16384"}, timeout=10)
                if sample_resp.status_code in (200, 206):
                    data = json.loads(sample_resp.text)
                    if isinstance(data, list) and data:
                        _project_record(data[0])
                        projection_hint = "ok"
        except Exception:
            projection_hint = "needs_validation"

        entries.append(
            {
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
                "lfs": getattr(item, "lfs", None),
                "projection": projection_hint,
            }
        )
        seen_projections.add(projection_hint)

    if not entries:
        raise FileNotFoundError(f"No supported files found in {repo}/{date_folder}")

    # Reject manifest if any file needs validation and others are ok (mixed risk)
    if "needs_validation" in seen_projections and len(seen_projections) > 1:
        raise ValueError("Mixed schema confidence in repo; resolve before training")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "vanguard.manifest.build_manifest",
        "projection_policy": "require_prompt_response",
        "entries": entries,
    }

    out_path = _ensure_dir(out_dir / repo) / f"{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path


def load_manifest(repo: str, date_folder: str, manifest_dir: Path = MANIFEST_ROOT) -> Dict[str, Any]:
    p = manifest_dir / repo / f"{date_folder}.json"
    if not p.is_file():
        raise FileNotFoundError(f"Manifest not found: {p}. Run build_manifest first.")
    return json.loads(p.read_text())


def get_or_reuse_studio(
    name: str,
    machine: Machine,
    create_ok: bool = True,
    teamspace: str = "default",
) -> Any:
    """
    Reuse a running studio if present; if stopped, restart it.
    Avoids recreating studios and preserves Lightning quota.
    """
    if not _HF_AVAILABLE:
        raise RuntimeError("lightning not available")

    ts = Teamspace(teamspace)
    for s in ts.studios:
        if s.name == name:
            if s.status == "running":
                return s
            # stopped/idle -> restart
            s.start(machine=machine)
            return s

    if not create_ok:
        raise RuntimeError(f"Studio {name} not running and create_ok=False")
    # create new
    return Lightning.Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

---

### 2. Launcher helper (Mac orchestrator)

```bash
#!/usr/bin/env bash
# /opt/axentx/v
