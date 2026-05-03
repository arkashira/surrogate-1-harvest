# vanguard / frontend

## Final Synthesis (adopts strongest, correct, actionable parts)

**Core diagnosis (merged, corrected)**
- No content-addressed manifest for dataset ingestion: frontend triggers re-listing of HF repos at runtime, causing 429 rate-limits and non-reproducible runs.
- Mixed-schema files from `dataset-mirror` land in `enriched/` without projection to `{prompt,response}`, breaking downstream training expectations.
- Training orchestration recreates Lightning Studios instead of reusing running ones, wasting quota.
- Data loading during training uses `load_dataset`/API calls instead of raw CDN URLs, hitting auth/rate-limits.
- Missing pre-flight file-list cache: no JSON manifest of date-scoped file paths to enable zero-API training runs.

**Chosen strategy (correct + actionable)**
- Build a single, content-addressed `file-list.json` via one API call (non-recursive tree) that contains CDN URLs only.
- Reuse a running Lightning Studio when available; create only if necessary.
- Project mixed-schema files to `{prompt,response}` during parse (streaming, minimal memory).
- Use CDN-only fetches during training (no HF API/auth).
- Keep frontend simple: call a backend endpoint that runs the manifest builder, then pass the manifest into the studio.

---

## 1. Manifest builder (single API call; correct + safe)

```bash
# /opt/axentx/vanguard/scripts/build-file-list.sh
#!/usr/bin/env bash
set -euo pipefail
# Usage: ./build-file-list.sh <hf_repo> <date_folder> <out_json>
# Example: ./build-file-list.sh axentx/dataset-mirror batches/mirror-merged/2026-05-03 file-list.json

HF_REPO="${1:-axentx/dataset-mirror}"
DATE_FOLDER="${2:-batches/mirror-merged/$(date +%Y-%m-%d)}"
OUT_JSON="${3:-file-list.json}"

python3 - "$HF_REPO" "$DATE_FOLDER" "$OUT_JSON" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
folder = sys.argv[2].rstrip("/")
out = sys.argv[3]

api = HfApi()
# recursive=False keeps one page (fast, low rate-limit cost)
items = api.list_repo_tree(repo=repo, path=folder, recursive=False)

files = []
for item in items:
    if item.rfilename.lower().endswith((".parquet", ".jsonl", ".json")):
        cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.rfilename}"
        files.append({"path": item.rfilename, "cdn": cdn})

os.makedirs(os.path.dirname(out) if os.path.dirname(out) else ".", exist_ok=True)
with open(out, "w") as f:
    json.dump({"repo": repo, "folder": folder, "files": files}, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build-file-list.sh
```

---

## 2. Lightning utilities (reuse studio + CDN-only dataloader)

```typescript
// /opt/axentx/vanguard/src/lib/lightning.ts
import type { Studio } from "lightning-ai"; // adjust to actual SDK

let _cachedStudio: Studio | null = null;

export async function getOrCreateStudio(name: string) {
  // Reuse running studio to save quota
  if (_cachedStudio && _cachedStudio.status === "running") {
    return _cachedStudio;
  }

  // In real usage, use Lightning SDK to list/create studios.
  // This is a minimal, safe placeholder that favors reuse.
  const teamspace = await (window as any).Lightning?.Teamspace?.current?.();
  if (!teamspace) {
    throw new Error("Lightning SDK not available");
  }

  const existing = teamspace.studios?.find(
    (s: Studio) => s.name === name && s.status === "running"
  );
  if (existing) {
    _cachedStudio = existing;
    return existing;
  }

  const studio = await teamspace.createStudio({
    name,
    machine: "L40S", // free-tier friendly; change if quota allows
    create_ok: true,
  });
  _cachedStudio = studio;
  return studio;
}

export function buildCdnDataModule(fileListPath: string) {
  // Returns a Lightning-compatible Python snippet that uses CDN URLs only.
  return `
from torch.utils.data import IterableDataset, DataLoader
import pyarrow.parquet as pq
import requests
import json
import os
import tempfile

class CdnParquetIterable(IterableDataset):
    def __init__(self, file_list_path):
        with open(file_list_path) as f:
            manifest = json.load(f)
        self.urls = [f["cdn"] for f in manifest["files"]]

    def __iter__(self):
        for url in self.urls:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                    for chunk in r.iter_content(chunk_size=8192):
                        tmp.write(chunk)
                    tmp_path = tmp.name
                try:
                    # Project to {prompt, response} (ignore mixed-schema extras)
                    table = pq.read_table(tmp_path, columns=["prompt", "response"])
                    for row in table.to_pylist():
                        yield {
                            "prompt": row.get("prompt") or "",
                            "response": row.get("response") or "",
                        }
                finally:
                    os.unlink(tmp_path)

def cdn_dataloader(file_list_path, batch_size=8):
    ds = CdnParquetIterable(file_list_path)
    return DataLoader(ds, batch_size=batch_size, num_workers=0)
`;
}
```

---

## 3. Frontend launcher (simple, backend-backed)

```typescript
// /opt/axentx/vanguard/src/components/TrainingLauncher.tsx
import React, { useState } from "react";
import { getOrCreateStudio, buildCdnDataModule } from "../lib/lightning";

export function TrainingLauncher() {
  const [dateFolder, setDateFolder] = useState("batches/mirror-merged/2026-05-03");
  const [status, setStatus] = useState("idle");

  const run = async () => {
    setStatus("building-file-list");
    try {
      // Call backend to run build-file-list.sh (one API call) and return manifest
      const res = await fetch("/api/build-file-list", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dateFolder }),
      });
      if (!res.ok) throw new Error("Failed to build file list");
      const manifest = await res.json();

      setStatus("launching-studio");
      const studio = await getOrCreateStudio("vanguard-train");

      if (studio.status !== "running") {
        await studio.start({ machine: "L40S" });
      }

      setStatus("submitting-run");
      await studio.upload("file-list.json", JSON.stringify(manifest, null, 2));
      await studio.upload(
        "train.py",
        `
import json
${buildCdnDataModule("file-list.json")}

# Minimal training placeholder (replace with real model/training loop)
loader = cdn_dataloader("file-list.json", batch_size=4)
for batch in loader:
    print("batch keys:", list(batch.keys()))
    # Replace with actual step
    break
print("CDN data load OK — training can proceed without HF API calls")
`
      );

      await studio.run("python train.py");
      setStatus("submitted");
    } catch (err) {
      console.error(err);
      setStatus("error");
    }
  };

  return (
    <div>
      <label>
        Date folder:
        <input value={dateFolder} onChange={(e) => setDateFolder(e.target.value)} />
      </label>
      <button onClick={run} disabled={status !== "idle"}>
        Launch training
      </button>
      <div>Status: {status}</div>
    </div>
  );
}

