# airship / discovery

### Final Synthesis (Best Parts + Correctness + Actionability)

**Highest-value incremental improvement (<2h):**  
**CDN-first ingestion + deterministic sharding**  
Eliminates HF API 429s during training and removes the 128-commit/hr bottleneck by:

1. Generating a deterministic file list once (or per date folder) and embedding it in training so Lightning Studio does **zero HF API calls** during data loading (CDN-only).
2. Routing dataset writes across 5 sibling repos by hashing the slug → deterministic shard (~640 writes/hr aggregate).

---

### Concrete Implementation Plan (prioritized, executable)

| Step | Owner | Time | Action |
|------|-------|------|--------|
| 1 | Orchestrator | 15m | One-time `list_repo_tree` for target date folder → save `file_list.json`. |
| 2 | Training script | 30m | Update loader to read embedded `file_list.json` and fetch via CDN URLs (`resolve/main/...`). |
| 3 | Ingestion writer | 30m | Add deterministic shard selector (5 repos) and write only `{prompt,response}` parquet to `batches/mirror-merged/{date}/{slug}.parquet`. |
| 4 | Studio guard | 15m | Reuse running Studio; restart only if stopped before `.run()`. |
| 5 | Smoke test | 30m | Run ingestion for one small date folder; verify CDN fetch in training; confirm no HF API calls during dataload. |

---

### Correct, Actionable Code Snippets

#### 1) Generate file list (run once per date folder)

```bash
#!/usr/bin/env bash
# scripts/generate_file_list.sh
set -euo pipefail

REPO="datasets/your-mirror-repo"
DATE_FOLDER="2026-05-01"
OUTFILE="file_list.json"

python3 - <<PY
import json, os
from huggingface_hub import HfApi

api = HfApi()
tree = api.list_repo_tree(
    repo_id=os.environ["REPO"],
    path=os.environ["DATE_FOLDER"],
    recursive=False
)
files = [f.rfilename for f in tree if f.type == "file" and f.rfilename.endswith(".parquet")]
with open(os.environ["OUTFILE"], "w") as f:
    json.dump(files, f, indent=2)
print(f"Wrote {len(files)} parquet files to {os.environ['OUTFILE']}")
PY
```

Embed `file_list.json` in the training package or pass as an artifact.

---

#### 2) Training: CDN-only loader (zero HF API during training)

```python
# train.py (excerpt)
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from torch.utils.data import IterableDataset

CDN_ROOT = "https://huggingface.co/datasets/your-mirror-repo/resolve/main"

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path):
        with open(file_list_path) as f:
            self.files = json.load(f)

    def _fetch_parquet(self, rfilename):
        url = f"{CDN_ROOT}/{rfilename}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return pq.read_table(BytesIO(resp.content))

    def __iter__(self):
        for rfn in self.files:
            if not rfn.endswith(".parquet"):
                continue
            table = self._fetch_parquet(rfn)
            for row in table.to_pylist():
                yield {
                    "prompt": row.get("prompt"),
                    "response": row.get("response"),
                }
```

No `load_dataset` or `list_repo_files` during training → zero HF API calls.

---

#### 3) Ingestion: deterministic shard + projection

```python
# ingest.py (excerpt)
import hashlib
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

SIBLINGS = [
    "datasets/your-mirror-repo",
    "datasets/your-mirror-repo-sib1",
    "datasets/your-mirror-repo-sib2",
    "datasets/your-mirror-repo-sib3",
    "datasets/your-mirror-repo-sib4",
]

def pick_repo(slug: str) -> str:
    digest = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLINGS[digest % len(SIBLINGS)]

def write_projected(batch_id, rows, date):
    # rows: list[dict] with at least {prompt, response, slug}
    by_repo = {}
    for r in rows:
        slug = r["slug"]
        repo = pick_repo(slug)
        by_repo.setdefault(repo, []).append({
            "prompt": r["prompt"],
            "response": r["response"],
        })

    for repo, items in by_repo.items():
        table = pa.Table.from_pylist(items)
        out_dir = Path(f"batches/mirror-merged/{date}")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{batch_id}.parquet"
        pq.write_table(table, out_path)
        # hf_api.upload_file(
        #     path_or_fileobj=out_path,
        #     path_in_repo=out_path.as_posix(),
        #     repo_id=repo,
        # )
```

- Writes contain **only** `{prompt, response}` (no extra metadata columns).
- Attribution is encoded in folder/`slug` filename pattern.
- Spread across 5 repos → ~640 writes/hr aggregate.

---

#### 4) Lightning Studio guard (reuse + restart)

```python
# lightning_launcher.py (excerpt)
from lightning import Teamspace, Studio, Machine

TEAMSPACE = "your-teamspace"
STUDIO_NAME = "surrogate-train-l40s"

def ensure_studio():
    team = Teamspace(TEAMSPACE)
    running = [s for s in team.studios if s.name == STUDIO_NAME and s.status == "Running"]
    if running:
        return running[0]

    stopped = [s for s in team.studios if s.name == STUDIO_NAME and s.status == "Stopped"]
    if stopped:
        studio = stopped[0]
        studio.start(machine=Machine.L40S)
        return studio

    return Studio.create(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        teamspace=TEAMSPACE,
        create_ok=True,
    )

def run_training(script_path, file_list_artifact):
    studio = ensure_studio()
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)
    studio.run(
        [
            "python",
            script_path,
            "--file-list",
            file_list_artifact,
        ]
    )
```

---

### Expected Outcome

- **Reduced HF API 429s** during ingestion and training.
- **Increased ingestion throughput** via deterministic sharding across 5 repos.
- **Stable, high-throughput pipeline** without quota thrashing.

**Estimated time to implement:** < 2 hours.
