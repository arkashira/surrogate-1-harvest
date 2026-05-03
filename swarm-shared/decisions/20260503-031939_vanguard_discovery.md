# vanguard / discovery

## Final Synthesized Implementation (Best of Both Candidates)

**Core synthesis decisions:**
- Use Candidate 1’s file layout (`scripts/`, `manifests/`) + Makefile integration for ops familiarity.
- Use Candidate 2’s CDN-first JSONL format (streaming-friendly, Lightning-safe) and `requests`-based API calls (lighter than full `huggingface_hub` dependency).
- Resolve schema/projection risk by embedding deterministic `sha256` in manifest (computed once during discovery) so training can trust content-addressed identity without re-querying HF.
- Keep verification strict (hash + size) and fail-fast to prevent silent drift.

---

### 1. File layout (new)
```
/opt/axentx/vanguard/
├── manifests/
│   └── YYYY-MM-DD.jsonl          # CDN-first, content-addressed manifest
├── scripts/
│   ├── discover_manifest.py      # one-shot Mac discovery (API → manifest)
│   └── verify_manifest.py        # local hash/size verification
├── requirements.txt              # +requests tqdm
└── Makefile                      # make manifest DATE=...
```

---

### 2. Requirements
```text
# /opt/axentx/vanguard/requirements.txt
requests>=2.31
tqdm>=4.66
```

---

### 3. Discovery script (CDN-first, JSONL)
```python
#!/usr/bin/env python3
# /opt/axentx/vanguard/scripts/discover_manifest.py
"""
Generate a CDN-first, content-addressed manifest for a date folder.
Usage:
  HF_REPO=datasets/axentx/surrogate-1 \
  DATE=2026-04-29 \
  python3 discover_manifest.py

Outputs: manifests/YYYY-MM-DD.jsonl
Each line:
  {"date":"2026-04-29","slug":"file.parquet","path":"2026-04-29/file.parquet",
   "cdn_url":"https://huggingface.co/datasets/.../resolve/main/...",
   "size":12345,"sha256":"..."}
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from tqdm import tqdm

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"
HEADERS = {"Accept": "application/json"}

if os.getenv("HF_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.getenv('HF_TOKEN')}"

def list_date_files(repo: str, date_folder: str) -> list[dict]:
    url = f"{HF_API_BASE}/repos/{repo}/tree/{date_folder}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 429:
        print("ERROR: Rate limited (429). Wait before retry.", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    items = resp.json()
    return [i for i in items if i.get("type") == "file"]

def sha256_of_cdn(cdn_url: str) -> str:
    # Deterministic fetch for hash; stream to avoid large memory.
    h = hashlib.sha256()
    with requests.get(cdn_url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN-first manifest.")
    parser.add_argument("--output", help="Output JSONL path (default: manifests/{DATE}.jsonl)")
    args = parser.parse_args()

    repo = os.getenv("HF_REPO")
    date_folder = os.getenv("DATE")
    if not repo or not date_folder:
        print("ERROR: Set HF_REPO and DATE env vars.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(__file__).parent.parent / "manifests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else (out_dir / f"{date_folder}.jsonl")

    print(f"Listing {repo}/{date_folder} ...")
    files = list_date_files(repo, date_folder)
    if not files:
        print("WARNING: No files found.", file=sys.stderr)

    manifest_date = datetime.now().isoformat(timespec="seconds")
    written = 0
    with out_path.open("w") as f:
        for item in tqdm(files, desc="Hashing"):
            slug = item["path"].replace(f"{date_folder}/", "")
            cdn_url = f"{HF_CDN_BASE}/{repo}/resolve/main/{item['path']}"
            sha256 = sha256_of_cdn(cdn_url)
            record = {
                "manifest_date": manifest_date,
                "date": date_folder,
                "slug": slug,
                "path": item["path"],
                "cdn_url": cdn_url,
                "size": item.get("size"),
                "sha256": sha256,
            }
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            written += 1

    print(f"Wrote {written} records to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 4. Verification script (strict)
```python
#!/usr/bin/env python3
# /opt/axentx/vanguard/scripts/verify_manifest.py
"""
Verify local files against a CDN-first manifest (hash + size).
Usage:
  python3 verify_manifest.py manifests/YYYY-MM-DD.jsonl /local/root
"""
import hashlib
import json
import sys
from pathlib import Path

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: verify_manifest.py MANIFEST.jsonl [local_root]", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path(sys.argv[1])
    local_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()

    ok = True
    with manifest_path.open() as f:
        for line in f:
            rec = json.loads(line)
            local_path = local_root / rec["slug"]
            if not local_path.exists():
                print(f"MISSING: {rec['slug']}")
                ok = False
                continue

            if rec.get("size") is not None and local_path.stat().st_size != rec["size"]:
                print(f"SIZE MISMATCH: {rec['slug']} (expected {rec['size']}, got {local_path.stat().st_size})")
                ok = False

            actual = sha256_file(local_path)
            if actual != rec["sha256"]:
                print(f"SHA256 MISMATCH: {rec['slug']}")
                ok = False

    if ok:
        print("All checks passed.")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
```

---

### 5. Makefile (simple, reproducible)
```makefile
# /opt/axentx/vanguard/Makefile
.PHONY: manifest
manifest:
	@HF_REPO=${HF_REPO} DATE=${DATE} python3 scripts/discover_manifest.py
```

---

### 6. Permissions
```bash
chmod +x /opt/axentx/vanguard/scripts/discover_manifest.py
chmod +x /opt/axentx/vanguard/scripts/verify_manifest.py
```

---

### 7. Verification plan (concrete)
1. Install deps:
   ```bash
   cd /opt/axentx/vanguard
   pip install -r requirements.txt
   ```
2. Run discovery (Mac side; after any rate-limit window):
   ```bash
   HF_REPO=datasets/axentx/surrogate-1 DATE=
