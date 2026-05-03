# surrogate-1 / frontend

## Decision — highest-value <2h frontend fix

Replace recursive `list_repo_files` + per-file API calls in the frontend/data-source layer with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.  
This directly applies the **HF CDN bypass** and **pre-list file paths** patterns from the playbook and removes the 429/rate-limit and OOM risks during dataset listing/streaming.

---

## Implementation plan (≤2h)

1. Locate frontend dataset listing/streaming code (likely in `src/` or `bin/` helpers used by the UI).  
2. Replace `list_repo_files(..., recursive=True)` with `list_repo_tree(path, recursive=False)` per date folder.  
3. Persist the file list to a small JSON (`file-list.json`) and embed/bundle it in the frontend (or fetch once from CDN).  
4. Switch data streaming to use raw CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header.  
5. Parse each file on the fly and project to `{prompt,response}` only (ignore extra schema columns).  
6. Add lightweight dedup hint using central md5 store (reuse `lib/dedup.py` pattern) if not already present.  
7. Verify locally with a small date folder; confirm zero API calls during streaming.

---

## Code snippets

### 1) Replace recursive listing with per-folder tree + CDN URLs

```python
# src/datasets/hf_client.py
import os
import json
import requests
from huggingface_hub import HfApi, hf_hub_download
from typing import List, Dict

HF_REPO = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

api = HfApi()

def list_date_folder(date_folder: str) -> List[str]:
    """
    List files in a single date folder (non-recursive) using tree API.
    Returns CDN-ready URLs.
    """
    items = api.list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        repo_type="dataset",
        recursive=False
    )
    # Keep only files we want to stream (parquet/jsonl)
    files = [it.rfilename for it in items if it.rfilename.endswith((".parquet", ".jsonl"))]
    return [f"{CDN_ROOT}/{date_folder}/{f}" for f in sorted(files)]

def save_file_list(date_folder: str, out_path: str = "public/file-list.json"):
    urls = list_date_folder(date_folder)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"date_folder": date_folder, "files": urls}, f, indent=2)
    return urls
```

### 2) Stream via CDN with schema projection

```python
# src/datasets/stream.py
import pyarrow.parquet as pq
import pyarrow as pa
import json
import os
import tempfile
import requests
from urllib.parse import urlparse

MD5_STORE_PATH = os.getenv("MD5_STORE_PATH", "lib/dedup.db")

def download_cdn_temp(url: str) -> str:
    """Download a single file from CDN to temp path; no auth header."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    suffix = os.path.splitext(urlparse(url).path)[1] or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(resp.content)
    return path

def project_to_pair(batch) -> Dict[str, str]:
    """Project record batch / table to {prompt, response} only."""
    prompt_col = None
    response_col = None
    # tolerant column names
    prompt_candidates = {"prompt", "input", "question", "instruction"}
    response_candidates = {"response", "output", "answer", "completion"}
    for c in batch.schema.names:
        if c in prompt_candidates:
            prompt_col = c
        if c in response_candidates:
            response_col = c
    if prompt_col is None or response_col is None:
        # fallback: first two string cols
        str_cols = [n for n in batch.schema.names if pa.types.is_string(batch.schema.field(n).type)]
        if len(str_cols) >= 2:
            prompt_col, response_col = str_cols[0], str_cols[1]
        else:
            raise ValueError("Cannot find prompt/response columns")
    return {
        "prompt": batch.column(prompt_col).to_pylist(),
        "response": batch.column(response_col).to_pylist(),
    }

def stream_cdn_files(file_urls: List[str], batch_size: int = 1024):
    """
    Stream files from CDN (no HF API auth) and yield {prompt, response} records.
    """
    from lib.dedup import DedupStore  # reuse central md5 store pattern
    dedup = DedupStore(MD5_STORE_PATH)

    for url in file_urls:
        path = download_cdn_temp(url)
        try:
            if url.endswith(".parquet"):
                pf = pq.ParquetFile(path)
                for batch in pf.iter_batches(batch_size=batch_size):
                    projected = project_to_pair(batch)
                    for p, r in zip(projected["prompt"], projected["response"]):
                        # simple dedup by content hash (optional)
                        h = str(hash(p + r))
                        if not dedup.seen(h):
                            dedup.add(h)
                            yield {"prompt": p, "response": r}
            elif url.endswith(".jsonl"):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        # tolerant projection
                        p = obj.get("prompt") or obj.get("input") or obj.get("question")
                        r = obj.get("response") or obj.get("output") or obj.get("answer")
                        if p is None or r is None:
                            continue
                        h = str(hash(p + r))
                        if not dedup.seen(h):
                            dedup.add(h)
                            yield {"prompt": p, "response": r}
        finally:
            if os.path.exists(path):
                os.remove(path)
```

### 3) Frontend usage (fetch pre-listed file-list once, then stream via CDN)

```javascript
// src/frontend/api.js
async function fetchFileList(dateFolder) {
  const res = await fetch(`/api/file-list?date=${dateFolder}`);
  if (!res.ok) throw new Error("Failed to fetch file list");
  return res.json(); // { files: [...] }
}

async function* streamPairs(fileUrls) {
  // In production, this would call a backend endpoint that runs stream_cdn_files
  // and yields NDJSON. Example:
  for (const url of fileUrls) {
    const res = await fetch(`/api/stream?url=${encodeURIComponent(url)}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (line) yield JSON.parse(line);
      }
    }
  }
}
```

---

## Verification checklist (quick)

- [ ] `list_repo_tree` used per date folder (no recursive `list_repo_files`).  
- [ ] CDN URLs used for downloads (no Authorization header).  
- [ ] Projection to `{prompt,response}` works for mixed-schema files.  
- [ ] Zero HF API calls during streaming (check logs).  
- [ ] Frontend can fetch pre-listed file list and stream via backend endpoint.

---

## Notes

- This change removes the primary cause of 429s during listing and reduces memory pressure (no heavy `datasets` streaming on limited runners).  
- Keep the central md5 dedup store (`lib/dedup.py`) as the source of truth for cross-run dedup; per-run
