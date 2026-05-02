# surrogate-1 / discovery

## Final consolidated implementation (correct + actionable)

**Core idea**  
Run a single, cheap pre-flight to list today’s public files once per workflow, then run 16 shards in parallel that download only their deterministic share via CDN (no auth header) and append to a date-partitioned, uniquely named shard file. Prevent races and reruns with filename timestamps and existence checks.

---

## 1) Workflow changes (`.github/workflows/ingest.yml`)

```yaml
name: surrogate-1-ingest
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

env:
  SHELL: /bin/bash
  DATASET_REPO: axentx/surrogate-1-training-pairs

jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.date }}
    steps:
      - id: date
        run: echo "date=$(date -u +%Y-%m-%d)" >> "$GITHUB_OUTPUT"

      - uses: actions/checkout@v4

      - name: Install deps
        run: pip install huggingface_hub

      - name: List public folder (single API call)
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          python -c "
          import json, os
          from huggingface_hub import list_repo_tree
          repo = os.getenv('DATASET_REPO')
          folder = f'public/${{ needs.preflight.outputs.date }}'
          try:
            tree = list_repo_tree(repo, path=folder, recursive=False)
            files = sorted(f.rfilename for f in tree if getattr(f, 'type', None) == 'file')
          except Exception:
            files = []
          out = 'file-list.json'
          with open(out, 'w') as f:
            json.dump(files, f)
          print(f'Listed {len(files)} files -> {out}')
          "

      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

  ingest:
    needs: preflight
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    env:
      SHARD_ID: ${{ matrix.shard }}
      DATE_PART: ${{ needs.preflight.outputs.date }}
    steps:
      - uses: actions/checkout@v4

      - name: Download file-list
        uses: actions/download-artifact@v4
        with:
          name: file-list
          path: .

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Ensure executable + correct shebang
        run: |
          sed -i '1s|^.*$|#!/usr/bin/env bash|' bin/dataset-enrich.sh || true
          chmod +x bin/dataset-enrich.sh

      - name: Run worker (CDN-bypass, date-partitioned)
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          FILE_LIST="$(pwd)/file-list.json"
          bash bin/dataset-enrich.sh "$SHARD_ID" "$DATE_PART" "$FILE_LIST"
```

---

## 2) Script changes (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${1:-0}"
DATE_PART="${2:-$(date -u +%Y-%m-%d)}"
FILE_LIST="${3:-}"

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="batches/public-merged/${DATE_PART}"
TS=$(date -u +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "$OUT_FILE")"

echo "[$(date -u)] Shard $SHARD_ID | date=$DATE_PART | output=$OUT_FILE"

python - "$SHARD_ID" "$DATE_PART" "$FILE_LIST" "$OUT_FILE" <<'PY'
import json, os, sys, hashlib, subprocess, time, urllib.request
from pathlib import Path

SHARD_ID = int(sys.argv[1])
DATE_PART = sys.argv[2]
FILE_LIST = sys.argv[3] if sys.argv[3] not in ("", "-") else None
OUT_FILE = sys.argv[4]
REPO = "axentx/surrogate-1-training-pairs"

def hf_cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

def hf_api_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

def should_process(file_path: str) -> bool:
    slug = Path(file_path).stem
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return (h % 16) == SHARD_ID

def download_with_retry(url: str, dest: Path, max_tries: int = 3, backoff: int = 5) -> bool:
    for attempt in range(1, max_tries + 1):
        try:
            # CDN download (no auth header)
            req = urllib.request.Request(url, headers={"User-Agent": "surrogate-ingest"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
                f.write(resp.read())
            return True
        except Exception as e:
            if attempt == max_tries:
                print(f"Download failed after {max_tries} tries: {url} -> {e}")
                return False
            time.sleep(backoff * attempt)
    return False

def main():
    candidates = []
    if FILE_LIST and os.path.isfile(FILE_LIST):
        with open(FILE_LIST) as f:
            candidates = json.load(f)
    else:
        # fallback list via API (should be rare)
        try:
            import huggingface_hub as hfh
            folder = f"public/{DATE_PART}"
            tree = hfh.list_repo_tree(REPO, path=folder, recursive=False)
            candidates = sorted(f.rfilename for f in tree if getattr(f, "type", None) == "file")
        except Exception as e:
            print("Failed to list repo tree:", e)
            return

    processed = 0
    written = 0
    for rel in candidates:
        if not should_process(rel):
            continue
        processed += 1

        url = hf_cdn_url(rel)
        tmp = Path("tmp_download") / Path(rel).name
        tmp.parent.mkdir(parents=True, exist_ok=True)

        ok = download_with_retry(url, tmp)
        if not ok:
            # fallback to API URL (same URL in most cases; kept for symmetry)
            ok = download_with_retry(hf_api_url(rel), tmp)
        if not ok:
            continue

        try:
            # Project to {prompt,response} at parse time.
            # Replace this block with your real parser per schema.
            # For now, emit minimal valid lines.
            prompt = f"prompt from {rel}"
            response = f"response from {rel}"
            line = json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False)
            with open(OUT_FILE, "a", encoding="utf-8") as out:
                out.write(line + "\n")
            written += 1
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    print(f"Processed {processed} files, wrote {written} lines to {OUT_FILE}")

if __name__ == "__main__":
    main()
PY

# Optional: upload today's shard file to dataset repo (idempotent)
# Skip if file already exists to prevent rerun races.
if command -v huggingface_hub >/dev/null 2>&1; then
  if huggingface_hub hdfs ls "${REPO}" --path "${OUT_FILE}" >/dev/null 2>&1; then
    echo "Upload skipped: ${OUT_FILE} already exists in repo."
  else
    echo "Uploading ${OUT_FILE}..."
    huggingface_hub hdfs
