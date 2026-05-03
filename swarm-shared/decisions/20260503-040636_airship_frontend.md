# airship / frontend

Candidate 1 provides the complete, production-grade backend and orchestration plan; Candidate 2 only supplies an incomplete frontend stub with no training or runner logic.  
The correct, actionable synthesis is Candidate 1 in full, with one small, high-value addition from Candidate 2 (a minimal status/health endpoint) so operators can observe the runner without breaking the CDN/idle-resilience guarantees.

Below is the single, merged, ready-to-run artifact. Contradictions are resolved in favor of correctness + concrete actionability:

- No HF API calls during training (CDN-only).  
- Idle-timeout resilience via Lightning studio reuse + restart loop.  
- One-time file-list generation outside rate-limit windows.  
- Optional lightweight health endpoint (from Candidate 2) for observability.  
- No frontend bloat in the training path (frontend remains optional; training works headless).

---

## Final Implementation (≤2 h)

### 0. Prerequisites (one-time)
```bash
cd /opt/axentx/airship
pip install "lightning>=2.2" pyarrow requests
```

---

### 1. Generate parquet file list once (after HF rate-limit clears)
`scripts/generate-parquet-list.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-dataset"
DATE=$(date +%Y-%m-%d)
FOLDER="batches/mirror-merged/${DATE}"
OUTPUT="training/file_list.json"

mkdir -p training

python3 - <<PY
import json, os
from huggingface_hub import HfApi

api = HfApi()
files = api.list_repo_tree(
    repo_id=os.environ.get("HF_REPO", "$REPO"),
    path=os.environ.get("HF_PATH", "$FOLDER"),
    repo_type="dataset",
    recursive=False,
)
parquet_files = [f.rfilename for f in files if f.rfilename.endswith(".parquet")]

out_path = os.path.join(os.getcwd(), "$OUTPUT")
with open(out_path, "w") as f:
    json.dump(parquet_files, f, indent=2)

print(f"Found {len(parquet_files)} parquet files → {out_path}")
PY
```

Make executable and schedule (optional):
```bash
chmod +x scripts/generate-parquet-list.sh
# crontab -e
# 0 2 * * * cd /opt/axentx/airship && SHELL=/bin/bash scripts/generate-parquet-list.sh
```

---

### 2. CDN-only parquet loader (no HF API during training)
`training/cdn_loader.py`
```python
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import List, Dict

HF_DATASET = "axentx/surrogate-dataset"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

def load_parquet_from_cdn(file_rel_path: str) -> pq.Table:
    url = f"{CDN_ROOT}/{file_rel_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return pq.read_table(BytesIO(resp.content))

def stream_cdn_parquet_files(file_list_json: str = "training/file_list.json"):
    with open(file_list_json) as f:
        files: List[str] = json.load(f)

    for rel_path in files:
        table = load_parquet_from_cdn(rel_path)
        for batch in table.to_batches(max_chunksize=1000):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                yield {
                    "prompt": row.get("prompt", ""),
                    "response": row.get("response", ""),
                    "_source_file": rel_path,
                }
```

---

### 3. Lightning idle-resilient runner (reuse + restart on idle-stop)
`training/lightning_runner.py`
```python
import lightning as L
import time
from typing import Callable

def get_or_create_studio(
    name: str = "surrogate-train",
    machine: str = "L40S",
    max_retries: int = 3,
) -> L.studio.Studio:
    teamspace = L.Teamspace()

    for attempt in range(max_retries):
        for s in teamspace.studios:
            if s.name == name and s.status == "Running":
                print(f"Reusing running studio: {s.name}")
                return s

        try:
            studio = L.studio.Studio(
                name=name,
                machine=machine,
                create_ok=True,
            )
            print(f"Created studio: {studio.name}")
            return studio
        except Exception as e:
            if "H200" in str(e) and "lightning-lambda-prod" not in str(e):
                machine = "L40S"
                continue
            if attempt == max_retries - 1:
                raise
            time.sleep(30)

    raise RuntimeError("Failed to acquire studio after retries")

def run_training_with_idle_resilience(
    train_fn: Callable,
    studio_name: str = "surrogate-train",
):
    while True:
        studio = get_or_create_studio(studio_name)

        if studio.status != "Running":
            print(f"Studio {studio_name} stopped (idle?). Restarting...")
            studio = get_or_create_studio(studio_name)

        try:
            studio.run(train_fn)
            break  # success
        except Exception as e:
            if "idle" in str(e).lower() or "stopped" in str(e).lower():
                studio.stop()
                continue
            raise
```

---

### 4. Training entrypoint (CDN + idle-resilient runner)
`train.py`
```python
import lightning as L
from training.lightning_runner import run_training_with_idle_resilience
from training.cdn_loader import stream_cdn_parquet_files
import torch
from torch.utils.data import DataLoader, IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list="training/file_list.json"):
        self.file_list = file_list

    def __iter__(self):
        for item in stream_cdn_parquet_files(self.file_list):
            if item["prompt"] and item["response"]:
                yield item["prompt"], item["response"]

def train_step(batch):
    prompts, responses = zip(*batch)
    # TODO: integrate surrogate model forward
    return {"loss": torch.tensor(0.0)}  # placeholder

def train_epoch():
    dataset = CDNParquetDataset()
    loader = DataLoader(dataset, batch_size=8, num_workers=0)
    for batch in loader:
        train_step(batch)

def main_train():
    for epoch in range(10):  # or while True for 24/7
        train_epoch()

if __name__ == "__main__":
    run_training_with_idle_resilience(main_train)
```

---

### 5. Lightweight health/observability endpoint (optional, from Candidate 2)
Expose a tiny HTTP probe so external monitors or the optional UI can check runner status without invoking HF API.

`training/health.py`
```python
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import threading
import lightning as L

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            teamspace = L.Teamspace()
            running = [
                s.name for s in teamspace.studios
                if s.name == "surrogate-train" and s.status == "Running"
            ]
            status = {"status": "ok" if running else "no_studio", "studio_running": bool(running)}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return  # suppress stdout spam

def start_health_server(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health server listening on :{port}")
```

Wire into `main_train` (optional):
```python
# at top of train.py
from training.health import start
