# vanguard / discovery

## 1. Diagnosis
- No CDN-first manifest exists; ingestion/training scripts likely still call `list_repo_tree`/`load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training jobs cannot pin exact data slices and reruns drift.
- No deterministic entrypoint to list a single date folder once and embed it; Mac orchestration still relies on live API calls during Lightning job launch.
- No lightweight verification that CDN URLs resolve before training starts; first failure happens deep in training epochs.
- Project has no `scripts/` or `manifests/` layout yet (empty repo), so discovery must bootstrap the minimal viable path.

## 2. Proposed change
Create `scripts/make-cdn-manifest.sh` + `manifests/` layout:
- `scripts/make-cdn-manifest.sh`: takes `REPO`, `DATE`, optional `OUTDIR`; calls HF API **once** with `list_repo_tree(path=DATE, recursive=False)`, emits `manifests/{REPO}/{DATE}.jsonl` with `{ "path": "...", "cdn_url": "...", "sha": "..." }`.
- `scripts/verify-cdn-manifest.py`: reads the JSONL, HEAD-checks each CDN URL, reports unresolvable paths.
- Add `Makefile` targets: `make manifests/$(REPO)/$(DATE).jsonl`, `make verify`.

Scope: 3 files, ~120 lines total. No runtime changes to training yet — this is pure discovery/infra.

## 3. Implementation

```bash
# Create layout
mkdir -p /opt/axentx/vanguard/{scripts,manifests}
cd /opt/axentx/vanguard
```

### `scripts/make-cdn-manifest.sh`
```bash
#!/usr/bin/env bash
# scripts/make-cdn-manifest.sh
# Usage: REPO=datasets/opus-mt EN=en-de DATE=2024-01-15 ./make-cdn-manifest.sh
set -euo pipefail

: "${REPO:?required, e.g. datasets/opus-mt}"
: "${DATE:?required, YYYY-MM-DD folder in repo}"
OUTDIR="${OUTDIR:-manifests}"
OUTFILE="${OUTDIR}/${REPO//\//-}/${DATE}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

# Single API call: list one folder non-recursive
# Requires HF_TOKEN only for private repos; public repos work without.
python3 - "$REPO" "$DATE" "$OUTFILE" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree

repo_id = sys.argv[1]
date_folder = sys.argv[2]
outfile = sys.argv[3]

# Use token only if provided; public repos work unauthed (higher rate limits for listing too).
token = os.environ.get("HF_TOKEN") or None

# One call, non-recursive
tree = list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False, token=token)

with open(outfile, "w", encoding="utf-8") as f:
    for node in tree:
        if node.type != "file":
            continue
        path = node.path
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        entry = {
            "path": path,
            "cdn_url": cdn_url,
            "sha": getattr(node, "oid", ""),  # may be empty for some trees
            "size": getattr(node, "size", None),
        }
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY

echo "CDN manifest written to $OUTFILE"
```

### `scripts/verify-cdn-manifest.py`
```python
#!/usr/bin/env python3
# scripts/verify-cdn-manifest.py
# Usage: python3 verify-cdn-manifest.py manifests/datasets-opus-mt/2024-01-15.jsonl
import json
import sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def check(entry, timeout=5):
    url = entry["cdn_url"]
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        ok = r.status_code == 200
        return (entry["path"], ok, r.status_code)
    except Exception as e:
        return (entry["path"], False, str(e))

def main(manifest_path, workers=20):
    with open(manifest_path, encoding="utf-8") as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(check, e): e for e in entries}
        for fut in as_completed(futures):
            path, ok, detail = fut.result()
            if not ok:
                failed.append((path, detail))
            else:
                sys.stdout.write(".")
                sys.stdout.flush()

    print(f"\nChecked {len(entries)} entries; {len(failed)} failures")
    for path, detail in failed[:20]:
        print(f"  FAIL: {path} -> {detail}")
    if len(failed) > 20:
        print(f"  ... and {len(failed)-20} more")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main(sys.argv[1])
```

### `Makefile`
```make
REPO ?= datasets/opus-mt
DATE ?= 2024-01-15
MANIFEST = manifests/$(subst /,-,$(REPO))/$(DATE).jsonl

.PHONY: all clean verify

all: $(MANIFEST)

$(MANIFEST):
	REPO=$(REPO) DATE=$(DATE) OUTDIR=manifests ./scripts/make-cdn-manifest.sh

verify: $(MANIFEST)
	python3 scripts/verify-cdn-manifest.py $(MANIFEST)

clean:
	rm -f $(MANIFEST)
```

Make scripts executable:
```bash
chmod +x scripts/make-cdn-manifest.sh scripts/verify-cdn-manifest.py
```

## 4. Verification

1. Generate a manifest (public repo example):
   ```bash
   cd /opt/axentx/vanguard
   REPO=datasets/opus-mt DATE=2024-01-15 make
   ```
   Expect: `manifests/datasets-opus-mt/2024-01-15.jsonl` created with one JSONL per file.

2. Verify CDN reachability:
   ```bash
   make verify
   ```
   Expect: dots printed while checking, then summary like `Checked N entries; 0 failures`. Any 404/timeout appears as FAIL lines.

3. Inspect manifest content:
   ```bash
   head -n 3 manifests/datasets-opus-mt/2024-01-15.jsonl
   ```
   Expect lines like:
   ```json
   {"path": "2024-01-15/en-de.txt", "cdn_url": "https://huggingface.co/datasets/opus-mt/resolve/main/2024-01-15/en-de.txt", "sha": "...", "size": 12345}
   ```

4. Confirm idempotency and single API call behavior: re-run `make` — script should overwrite same file and not list recursive trees.

This gives the project a deterministic, CDN-first manifest that can be embedded into Lightning training scripts (zero API calls during data loading) and satisfies discovery requirements within the 2-hour window.
