# vanguard / discovery

## Final Synthesis (Best Parts + Corrected Contradictions)

**Core diagnosis (merged, non-redundant):**  
- No build-time deterministic asset manifest → frontend and training rely on runtime HF API (429 risk, breaks CDN-first).  
- No mount/entrypoint boundary between orchestration host (Mac) and compute target (Lightning) → heavy ops can run on Mac, violating “Mac=CLI only.”  
- No pre-listed file inventory per date folder → training cannot do CDN-only fetches; still uses `list_repo_files`/`load_dataset` at runtime.  
- No content-hashed CDN URLs or integrity verification → silent corruption possible and cache inefficiency.  

**Chosen approach (correctness + concrete actionability):**  
- Build a **single-shot manifest generator** (HF API once per date folder) producing CDN-first, content-hashed entries.  
- Provide a **frontend resolve utility** that consumes the manifest at build/dev time (zero runtime HF API).  
- Enforce a **strict entrypoint boundary** that blocks heavy compute on Mac unless explicitly allowed.  
- Update training stub to accept `--file-list` (manifest) and perform **CDN-only, integrity-checked fetches** during training.  

**Resolved contradictions in favor of correctness + actionability:**  
- Candidate 1’s `sha256_of_url` streams via CDN (correct: bypasses API rate limits), but it’s slow for huge files. We keep it optional (`--skip-hashes`) for speed in CI, but default to on for correctness in dev/local.  
- Candidate 1’s entrypoint grep-based block is fragile (static text matches). We keep the intent (block heavy ops on Mac) but make it actionable and explicit via `ALLOW_MAC_HEAVY` and a clear error.  
- Candidate 2 emphasizes integrity verification; we include SHA256 in the manifest and wire it into training fetches (actionable: verify on download).  
- Both agree on manifest schema and CDN-first URLs; we adopt that exactly.

---

## 1. Build Manifest (unchanged from Candidate 1, correct)

```bash
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
"""
Usage (Mac/CI):
  HF_TOKEN=hf_xxx python build_manifest.py \
    --repo datasets/axentx/vanguard-data \
    --date-folder 2026-05-03 \
    --out dist/manifest-2026-05-03.json

Produces CDN-first manifest with optional content hashes.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_of_url(url: str) -> str:
    import requests
    h = hashlib.sha256()
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, date_folder: str, out_path: Path, skip_hashes: bool = False):
    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Single API call: non-recursive list for this folder only.
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    manifest = []
    for node in tree:
        if node.type != "file":
            continue
        path = f"{date_folder}/{node.path.split('/')[-1]}"
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        entry = {
            "slug": node.path.split('/')[-1],
            "cdn_url": cdn_url,
            "size": node.size or 0,
            "sha256": None,
        }
        if not skip_hashes:
            try:
                entry["sha256"] = sha256_of_url(cdn_url)
            except Exception as e:
                print(f"Warning: could not hash {cdn_url}: {e}", file=sys.stderr)
        manifest.append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo": repo, "date_folder": date_folder, "generated_by": "build_manifest", "files": manifest}, f, indent=2)
    print(f"Wrote {len(manifest)} entries to {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, help="e.g. datasets/axentx/vanguard-data")
    p.add_argument("--date-folder", required=True, help="e.g. 2026-05-03")
    p.add_argument("--out", required=True, help="Output JSON path")
    p.add_argument("--skip-hashes", action="store_true", help="Skip CDN hashing (faster)")
    args = p.parse_args()
    build_manifest(args.repo, args.date_folder, Path(args.out), skip_hashes=args.skip_hashes)
```

---

## 2. Frontend Resolve Utility (unchanged from Candidate 1, correct)

```bash
# /opt/axentx/vanguard/frontend/resolve_asset.py
#!/usr/bin/env python3
"""
Frontend build/dev utility: resolve asset by slug using local manifest.
Usage:
  python resolve_asset.py --manifest dist/manifest-2026-05-03.json --slug file.parquet
Outputs JSON: {"cdn_url": "...", "sha256": "...", "size": 123}
"""
import argparse
import json
import sys
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--slug", required=True)
    args = p.parse_args()

    with open(args.manifest) as f:
        data = json.load(f)

    for item in data.get("files", []):
        if item["slug"] == args.slug:
            print(json.dumps({
                "cdn_url": item["cdn_url"],
                "sha256": item["sha256"],
                "size": item["size"],
            }))
            return

    print(f'{{"error": "slug {args.slug} not found in manifest"}}', file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
```

---

## 3. Entrypoint Boundary (improved clarity + actionability)

```bash
# /opt/axentx/vanguard/scripts/entrypoint.sh
#!/usr/bin/env bash
# Strict boundary between orchestration host (Mac) and compute target (Lightning/Space/local).
# Usage:
#   RUN_ENV=lightning ./entrypoint.sh python train_stub.py --file-list dist/manifest-2026-05-03.json
#
# Environment:
#   RUN_ENV=lightning|space|local   (required)
#   ALLOW_MAC_HEAVY=true            (opt-in to run heavy ops on Mac; not recommended)

set -euo pipefail

RUN_ENV="${RUN_ENV:-local}"
ALLOW_MAC_HEAVY="${ALLOW_MAC_HEAVY:-false}"

case "$RUN_ENV" in
  lightning|space|local) ;;
  *)
    echo "ERROR: RUN_ENV must be one of lightning|space|local (got: $RUN_ENV)" >&2
    exit 1
    ;;
esac

# Detect Mac and block heavy ops unless explicitly allowed.
if [[ "$(uname)" == "Darwin" ]] && [[ "$ALLOW_MAC_HEAVY" != "true" ]]; then
  # Basic heuristic: if any argument is a .py file, scan for heavy patterns.
  for arg in "$@"; do
    if [[ -f "$arg" && "$arg" == *.py ]]; then
      if grep -q -E "from_pretrained|torch\.compile|Trainer|trainer\.fit|model\.save_pretrained" "$arg"; then
        echo "ERROR: Heavy compute pattern detected on Mac in $arg. Set ALLOW_MAC_HEAVY=true to bypass (not recommended)." >&2
        exit 1
      fi
    fi
  done
fi

echo "RUN_ENV=$RUN_ENV — proceeding: $
