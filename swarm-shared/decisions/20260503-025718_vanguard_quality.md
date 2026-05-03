# vanguard / quality

## Final Synthesis — One Correct, Actionable Plan

**Core principle:** Eliminate runtime Hugging Face API calls during frontend load and training data ingest; enforce deterministic, content-hashed assets with CDN-first delivery and integrity checks.

---

### 1. Diagnosis (merged + resolved)

- **No build-time deterministic asset manifest** → frontend cannot resolve hashed bundles or CDN paths without runtime discovery, risking 429s and cache misses.  
- **No mount-point/entrypoint boundary** between orchestration host (Mac) and browser runtime → CDN-first strategy is not enforced; runtime HF API calls can leak in training or UI code.  
- **No content-hashed filenames** (training artifacts and frontend bundles) → breaks long-term CDN caching and forces re-ingestion on unchanged content.  
- **No pre-flight freeze of training file list** → training runs can trigger mid-run HF API calls and 429s.  
- **No guardrails against `load_dataset(streaming=True)` or recursive `list_repo_files`** → high risk of schema/rate-limit failures.  
- **Missing early-hints/preload for critical bundles** → harms TTI on repeat visits.  
- **No SRI metadata in HTML** → browser cannot validate CDN assets.

---

### 2. Concrete Changes (single source of truth)

1. **Add build-time manifest generator**  
   - Path: `/opt/axentx/vanguard/scripts/build_manifest.py` (new, executable)  
   - Produces: `frontend/dist/manifest.json` mapping logical names → `{file, hash, cdn_url, integrity}`  
   - Also produces: `training/file_list.json` (frozen) for the training run.

2. **Update Vite config**  
   - Path: `/opt/axentx/vanguard/frontend/vite.config.js`  
   - Enforce content-hashed output filenames.  
   - Inject manifest into `index.html` at build time and provide a strict runtime fallback.  
   - Set deterministic `base` for CDN assets.

3. **Update HTML entry**  
   - Path: `/opt/axentx/vanguard/frontend/index.html`  
   - Add SRI placeholders and preload hints for critical bundles using manifest values.

4. **Update training script**  
   - Path: `/opt/axentx/vanguard/training/train.py`  
   - Consume frozen `file_list.json` and fetch only via CDN (no HF API/token usage during training).  
   - Validate file availability at startup; fail fast if manifest or files missing.

---

### 3. Implementation

#### 3.1 Build manifest generator (deterministic + CI-friendly)

```python
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
"""
Generate deterministic asset manifest for CDN-first delivery.
Run during CI/build before vite build and before training starts.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
MANIFEST_PATH = FRONTEND_DIST / "manifest.json"
TRAIN_FILELIST_PATH = PROJECT_ROOT / "training" / "file_list.json"

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1")
HF_CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
CDN_BASE = os.getenv("AXENTX_CDN_BASE", "https://cdn.axentx.dev/vanguard")


def sha256_hex(path: Path, chunk_size=8192) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def base64_url_encode_sha256(hex_digest: str) -> str:
    # SRI-friendly: sha256-<base64url>
    raw = bytes.fromhex(hex_digest)
    import base64
    return "sha256-" + base64.b64encode(raw).decode("utf-8").rstrip("=").replace("+", "-").replace("/", "_")


def build_frontend_manifest() -> dict:
    manifest = {}
    if not FRONTEND_DIST.exists():
        return manifest

    for f in FRONTEND_DIST.rglob("*"):
        if f.is_file() and not f.name.endswith(".map") and not f.name.endswith(".json"):
            try:
                rel = f.relative_to(FRONTEND_DIST)
                h = sha256_hex(f)
                sri = base64_url_encode_sha256(h)
                cdn_url = f"{CDN_BASE}/{rel}?v={h[:16]}"
                manifest[str(rel)] = {
                    "file": str(rel),
                    "hash": h,
                    "hash_short": h[:16],
                    "cdn_url": cdn_url,
                    "integrity": sri,
                }
            except Exception:
                continue
    return manifest


def build_training_file_list(date_folder: str) -> dict:
    """
    Produce frozen file list for surrogate-1 training.
    In CI, run once (with HF API access) and commit/check-in or inject as build artifact.
    This function is a template; CI should populate files by calling HF API once.
    """
    # Example CI step:
    # huggingface-cli repo tree --repo-type dataset {HF_DATASET_REPO} --path batches/mirror-merged/{date_folder} --recursive
    # Save JSON to TRAIN_FILELIST_PATH
    return {
        "dataset_repo": HF_DATASET_REPO,
        "date_folder": date_folder,
        "generated_by": "scripts/build_manifest.py",
        "cdn_base": HF_CDN_BASE,
        "files": [],  # CI must populate
    }


def inject_manifest_into_html(manifest: dict) -> None:
    html_path = PROJECT_ROOT / "frontend" / "index.html"
    if not html_path.exists():
        return

    html = html_path.read_text()
    payload = json.dumps(manifest).replace("</", "\\u003c/")

    # Ensure placeholder exists
    if "__AXENTX_MANIFEST__" not in html:
        html = html.replace(
            "</head>",
            '  <script>window.__AXENTX_MANIFEST__ = {};</script>\n</head>',
            1,
        )

    # Replace placeholder with actual manifest
    html = html.replace(
        'window.__AXENTX_MANIFEST__ = {};',
        f'window.__AXENTX_MANIFEST__ = {payload};',
    )

    # Add preload hints for critical JS/CSS from manifest (best-effort)
    critical_keys = [k for k in manifest.get("frontend", {}) if k.endswith((".js", ".css"))]
    preloads = []
    for k in critical_keys[:6]:  # limit to avoid huge head
        entry = manifest["frontend"][k]
        if k.endswith(".js"):
            preloads.append(f'<link rel="modulepreload" href="{entry["cdn_url"]}" integrity="{entry["integrity"]}" crossorigin>')
        elif k.endswith(".css"):
            preloads.append(f'<link rel="preload" href="{entry["cdn_url"]}" as="style" integrity="{entry["integrity"]}" crossorigin>')

    if preloads and "<head>" in html:
        preload_block = "\n  ".join(preloads)
        html = html.replace("<head>", f"<head>\n  {preload_block}")

    html_path.write_text(html)


def main() -> None:
    date_folder = os.getenv("TRAIN_DATE", "2026-05-03")
    FRONTEND_DIST.mkdir(parents=True, exist_ok=True)

    frontend_manifest = build_frontend_manifest()
    training_file_list = build_training_file_list(date_folder)

    manifest = {
        "frontend": frontend_manifest,
        "training": training_file_list,
        "generated_at": subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]).decode().strip(),
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {MANIFEST_PATH}")

    inject
