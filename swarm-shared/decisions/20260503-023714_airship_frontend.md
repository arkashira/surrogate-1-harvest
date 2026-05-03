# airship / frontend

## Final Synthesis — Highest-Value, Actionable Plan

**Goal**: Eliminate HF API 429s during Surrogate training **and** improve local operability for devs, with zero backend changes and <2h total effort.

**Chosen scope (combined)**:
1. **CDN-first ingestion + Studio reuse** (Candidate 1) — fixes quota waste and 429s.
2. **Local “Surrogate AI status + quick actions” widget in Arkship UI** (Candidate 2) — improves dev UX and debugging speed.

These are orthogonal: one is infra/training, one is frontend operability. Both ship fast and reinforce the “Arkship uses Surrogate as AI backend” architecture.

---

## 1) CDN-first ingestion + Studio reuse (training side)

**Why this wins**:
- Removes `load_dataset(streaming=True)` HF API calls during training.
- Uses deterministic sibling routing to spread read load.
- Reuses running Lightning Studios instead of recreating them (quota + time savings).
- No new UI or infra; single-PR changes to `surrogate/ingest.py` and `surrogate/train.py`.

### Concrete implementation (refined from Candidate 1)

#### surrogate/ingest.sh (orchestration)
```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="axentx/surrogate-datasets"
DATE=$(date +%Y-%m-%d)
OUT_DIR="batches/mirror-merged/${DATE}"
mkdir -p "${OUT_DIR}"

# Pre-list once (reduces repeated API calls)
python - <<PY
import json, os
from huggingface_hub import list_repo_tree
REPO = os.getenv("REPO")
DATE = os.getenv("DATE")
tree = list_repo_tree(REPO, path=DATE, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith((".jsonl", ".parquet", ".json"))]
with open("file_list.json", "w") as f:
    json.dump({"date": DATE, "files": files}, f, indent=2)
print(f"Listed {len(files)} files -> file_list.json")
PY

# Ingest via CDN
python surrogate/ingest.py --date "${DATE}" --file-list file_list.json --out-dir "${OUT_DIR}"
```

#### surrogate/ingest.py (CDN-first, deterministic siblings)
```python
import argparse, hashlib, json, os
from pathlib import Path
from huggingface_hub import hf_hub_download
import pandas as pd
import pyarrow.parquet as pq

HF_REPO = "axentx/surrogate-datasets"
SIBLINGS = [f"axentx/surrogate-datasets-{i}" for i in range(5)]

def pick_sibling(slug: str) -> str:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLINGS[h % len(SIBLINGS)]

def project_record(raw) -> dict:
    return {
        "prompt": raw.get("prompt") or raw.get("input") or "",
        "response": raw.get("response") or raw.get("output") or "",
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--file-list", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(args.file_list) as f:
        meta = json.load(f)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    for path in meta["files"]:
        slug = Path(path).stem
        target_repo = pick_sibling(slug)
        local_path = hf_hub_download(
            repo_id=HF_REPO,
            filename=path,
            repo_type="dataset",
            cache_dir=".cache",
        )
        if path.endswith(".parquet"):
            df = pq.read_table(local_path).to_pandas()
            records = [project_record(r) for r in df.to_dict(orient="records")]
        else:
            records = []
            with open(local_path) as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    records.append(project_record(json.loads(line)))

        out_df = pd.DataFrame(records)
        out_file = out / f"{slug}.parquet"
        out_df.to_parquet(out_file, index=False)
        print(f"Projected {len(records)} records -> {out_file} (sibling={target_repo})")

if __name__ == "__main__":
    main()
```

#### surrogate/train.py (CDN loader + Studio reuse)
```python
import json, os, time
from pathlib import Path
from huggingface_hub import HfApi
import torch
from datasets import load_dataset
from lightning import Studio, Teamspace, Machine

HF_REPO = "axentx/surrogate-datasets"
FILE_LIST = os.getenv("FILE_LIST", "file_list.json")

def cdn_dataset(file_list_path: str):
    with open(file_list_path) as f:
        meta = json.load(f)
    data_files = [
        f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{p}"
        for p in meta["files"]
    ]
    ds = load_dataset("parquet", data_files={"train": data_files}, split="train")
    ds = ds.select_columns(["prompt", "response"])
    return ds

def reuse_or_create_studio(name: str):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running Studio: {name}")
            return s
    print(f"Creating Studio: {name}")
    return Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True,
    )

def main():
    studio = reuse_or_create_studio("surrogate-train")
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)
        while studio.status != "Running":
            time.sleep(10)
            studio.refresh()

    dataset = cdn_dataset(FILE_LIST)
    print(f"Loaded {len(dataset)} examples via CDN")

    # Replace with real training step / Trainer
    studio.run(lambda: print("Training on CDN dataset..."))

if __name__ == "__main__":
    main()
```

#### Observability & fallbacks
- Exponential backoff + max 3 retries on CDN fetch failures.
- Log: repo, sibling chosen, file count, Studio reuse hits.

---

## 2) Arkship UI: Surrogate AI status + quick actions widget

**Why this wins**:
- Improves developer experience and debugging speed.
- Uses existing microservices setup (`docker-compose.microservices.yml`).
- No backend changes; frontend-only widget.

### Concrete implementation (frontend)

#### Location
Add to `arkship/src/components/SurrogateStatusWidget.tsx` (or similar), and mount it in the main Arkship layout (e.g., top-right or sidebar).

#### Widget behavior
- Polls `http://localhost:8001/health` (or `/status`) every 10–15s.
- Shows:
  - Status: Running / Stopped / Unreachable
  - Uptime (if available)
  - Quick actions:
    - Restart surrogate container
    - Open surrogate UI (port 8001)
    - Tail surrogate logs (opens terminal pane or fetches logs via docker API)

#### Minimal React example
```tsx
// arkship/src/components/SurrogateStatusWidget.tsx
import React, { useEffect, useState } from "react";

const SURROGATE_URL = "http://localhost:8001";
const HEALTH_URL = `${SURROGATE_URL}/health`;

export function SurrogateStatusWidget() {
  const [status, setStatus] = useState<"running" | "stopped" | "unreachable">("unreachable");
  const [uptime, setUptime] = useState<string | null>(null);

  const checkHealth = async () => {
    try {
      const res = await fetch(HEALTH_URL, { signal: AbortSignal.timeout(3000) });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        setStatus("running");
        setUptime(data.uptime || null);
      } else {
        setStatus("st
