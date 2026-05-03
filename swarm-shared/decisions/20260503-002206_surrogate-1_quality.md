# surrogate-1 / quality

## Final Implementation Plan  
**Goal:** Eliminate HF API rate limits during training while keeping ingestion unchanged and enabling reproducible, CDN-only data loads.  
**Scope:** ≤2h implementation on orchestrator (Mac) + small loader change.

---

### 1) Add `bin/snapshot.sh` (single source of truth)

**Behavior**
- One non-recursive `list_repo_tree(recursive=False)` per date folder.  
- Deterministic, sorted output → identical inputs → identical manifest.  
- Shard-aware: includes `shards` mapping so runners can filter locally without extra API calls.  
- Exits non-zero on failure; prints JSON to stdout or `--output`.

**Manifest schema** (combines strengths)
```json
{
  "date": "2026-04-29",
  "repo": "datasets/axentx/surrogate-1-training-pairs",
  "created_at": "2026-04-29T14:03:00Z",
  "cdn_base": "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main",
  "base_path": "batches/public-merged/2026-04-29",
  "sha256": "<hex>",
  "files": [
    {
      "path": "batches/public-merged/2026-04-29/shard0-140300.jsonl",
      "cdn_url": "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/batches/public-merged/2026-04-29/shard0-140300.jsonl",
      "size": 12345678,
      "md5": null,
      "etag": null
    }
  ],
  "shards": {
    "0": ["batches/public-merged/2026-04-29/shard0-140300.jsonl", ...],
    ...
  }
}
```
- `sha256` is over canonical JSON (sorted keys, no whitespace) for integrity.  
- Keep `md5`/`etag` when available; `null` otherwise.

**CLI**
```bash
# Required
HF_TOKEN=hf_xxx bin/snapshot.sh --date 2026-04-29

# Optional
--repo axentx/surrogate-1-training-pairs
--shard 0          # filter manifest to one shard
--output snapshot-2026-04-29.json
```

**Implementation** (`bin/snapshot.sh`)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:?ERROR: HF_TOKEN required}"
DATE=""
SHARD=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --shard) SHARD="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -z "$DATE" ]] && { echo "ERROR: --date required" >&2; exit 1; }

BASE_PATH="batches/public-merged/${DATE}"
TMP=$(mktemp)

python3 - "$REPO" "$BASE_PATH" "$HF_TOKEN" "$TMP" <<'PY'
import json, sys, hashlib, datetime
from huggingface_hub import HfApi

repo_id, path, token, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
api = HfApi(token=token)
tree = api.list_repo_tree(repo_id=repo_id, path=path, recursive=False)

files = []
shards = {str(i): [] for i in range(16)}

for item in tree:
    if getattr(item, "type", None) != "file":
        continue
    p = item.path
    if not (p.endswith(".jsonl") or p.endswith(".parquet")):
        continue
    cdn = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{p}"
    entry = {
        "path": p,
        "cdn_url": cdn,
        "size": getattr(item, "size", None),
        "md5": getattr(item, "lfs", {}).get("oid", None) if hasattr(item, "lfs") else None,
        "etag": getattr(item, "etag", None)
    }
    files.append(entry)

    slug = p.split("/")[-1].rsplit(".", 1)[0]
    shard_id = str(abs(hash(slug)) % 16)
    if shard_id in shards:
        shards[shard_id].append(p)

files.sort(key=lambda x: x["path"])
for k in shards:
    shards[k].sort()

result = {
    "date": path.split("/")[-1],
    "repo": repo_id,
    "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    "cdn_base": f"https://huggingface.co/datasets/{repo_id}/resolve/main",
    "base_path": path,
    "files": files,
    "shards": shards
}

# canonical sha256
canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
result["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()

with open(out, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, sort_keys=False)
PY

# Optional shard filter (post-process)
if [[ -n "$SHARD" ]]; then
  python3 - "$TMP" "$SHARD" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
sid = sys.argv[2]
d["files"] = [f for f in d["files"] if f"shard{sid}-" in f["path"]]
with open(sys.argv[1], "w") as f:
    json.dump(d, f, indent=2)
PY
fi

if [[ -n "$OUTPUT" ]]; then
  mv "$TMP" "$OUTPUT"
  echo "Snapshot: $OUTPUT" >&2
else
  cat "$TMP"
  rm -f "$TMP"
fi
```
Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Add `bin/snapshot-verify.sh` (optional, fast)

Verifies manifest integrity and CDN reachability (HEAD requests; no auth).

```bash
#!/usr/bin/env bash
set -euo pipefail
MANIFEST="${1:?Usage: $0 <manifest.json>}"

python3 - "$MANIFEST" <<'PY'
import json, sys, hashlib, urllib.request

with open(sys.argv[1]) as f:
    m = json.load(f)

# 1) canonical sha256 check
copy = dict(m)
expected = copy.pop("sha256")
canonical = json.dumps(copy, sort_keys=True, separators=(",", ":"))
actual = hashlib.sha256(canonical.encode()).hexdigest()
assert actual == expected, f"sha256 mismatch: {actual} != {expected}"

# 2) lightweight HEAD checks (sample first 5)
for entry in m["files"][:5]:
    req = urllib.request.Request(entry["cdn_url"], method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                print(f"WARN: {entry['path']} -> HTTP {resp.status}")
    except Exception as e:
        print(f"WARN: {entry['path']} -> {e}")

print("OK: manifest valid and CDN reachable (sampled)")
PY
```
Make executable:
```bash
chmod +x bin/snapshot-verify.sh
```

---

### 3) Update training/loader to prefer CDN (zero HF API)

Add a small, optional `CDNJsonlDataset`. When `--manifest` is provided, use CDN only. Keep existing
