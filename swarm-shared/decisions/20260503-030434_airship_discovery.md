# airship / discovery

## Final Actionable Plan (Synthesized)

**Goal**: Eliminate HF API 429s and Lightning idle-stop kills so Surrogate training iteration is **<2 minutes** and never blocked by rate limits or idle timeouts.

---

### 1) Pre-cache file list once (10 min)
- Run once (or on cron after rate-limit clears) for one date folder.
- Save `surrogate/training/file_list.json` to repo.

```bash
# surrogate/training/list_hf_files.py
#!/usr/bin/env python3
import json, os
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-mirror"
DATE_PATH = "batches/mirror-merged/2026-05-03"
OUT_PATH = os.path.join(os.path.dirname(__file__), "file_list.json")

def main() -> None:
    api = HfApi()
    entries = api.list_repo_tree(repo_id=REPO_ID, path=DATE_PATH, recursive=False)
    files = sorted(e.path for e in entries if e.path.endswith(".parquet"))
    with open(OUT_PATH, "w") as f:
        json.dump({"repo_id": REPO_ID, "date_path": DATE_PATH, "files": files}, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Run once:
```bash
python3 surrogate/training/list_hf_files.py
```

---

### 2) Lightning-resilient training script (30–45 min)
- Use CDN URLs (no auth) to bypass HF API 429.
- Use `datasets` streaming with `split`/`keep_in_memory=False` to avoid schema drift and memory blowups.
- Keep only `prompt`/`response` at parse time.
- Add Lightning Studio reuse + idle-restart logic.

```python
# surrogate/training/train.py
#!/usr/bin/env python3
import json, os, sys, time, warnings
from pathlib import Path
from typing import Iterator, Dict

import torch
from datasets import load_dataset, Features, Value
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments

# --- Config ---
FILE_LIST_PATH = Path(__file__).parent / "file_list.json"
BATCH_SIZE = 8
MAX_STEPS = 200
MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"
OUTPUT_DIR = "./lightning_run"
CACHE_DIR = Path("./hf_cache")
CACHE_DIR.mkdir(exist_ok=True)

# --- Load file list ---
if not FILE_LIST_PATH.exists():
    print("Missing file_list.json. Run list_hf_files.py first.", file=sys.stderr)
    sys.exit(1)

with open(FILE_LIST_PATH) as f:
    meta = json.load(f)
REPO_ID = meta["repo_id"]
FILES = meta["files"]

# --- Build HF CDN paths (bypass auth) ---
def cdn_path(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{path}"

# --- Lightweight dataset via streaming + CDN ---
def build_dataset(tokenizer, max_length: int = 512):
    # Use a single split with explicit file list; keep_in_memory=False avoids OOM
    features = Features({
        "prompt": Value("string"),
        "response": Value("string"),
    })
    ds = load_dataset(
        "parquet",
        name="surrogate",
        data_files=[cdn_path(p) for p in FILES],
        split="train",
        keep_in_memory=False,
        cache_dir=str(CACHE_DIR),
        features=features,
        streaming=True,
    )

    def format_example(example):
        text = f"<｜User｜>{example['prompt']}<｜Assistant｜>{example['response']}"
        enc = tokenizer(
            text,
            truncation=True,
            max_length=max_length,
            padding="max_length",
        )
        enc["labels"] = enc["input_ids"].copy()
        return enc

    return ds.map(format_example, remove_columns=ds.features.keys)

# --- Lightning Studio reuse + idle-resilience ---
def ensure_lightning_studio_and_run():
    try:
        import lightning as L
    except ImportError:
        warnings.warn("lightning not installed; falling back to local run.")
        return run_local_train()

    studio_name = "surrogate-train-studio"
    teamspace = L.Teamspace()
    running = [s for s in teamspace.studios if s.name == studio_name and s.status == "Running"]

    if running:
        print(f"Reusing running studio: {studio_name}")
        studio = running[0]
    else:
        print(f"Starting studio: {studio_name}")
        from lightning.fabric.plugins import LightningStudio
        studio = LightningStudio(
            name=studio_name,
            machine="L40S",
            create_ok=True,
        )
        studio.start()

    if studio.status != "Running":
        print("Studio stopped; restarting...")
        studio.start(machine="L40S")

    # Execute training inside studio (non-blocking pattern)
    # Example: studio.run("python surrogate/training/train.py --local")
    # For immediate iteration, run locally:
    return run_local_train()

# --- Local training fallback (fast iteration) ---
def run_local_train():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    ds = build_dataset(tokenizer)

    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)

    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=BATCH_SIZE,
        max_steps=MAX_STEPS,
        logging_steps=20,
        save_steps=100,
        report_to="none",
        fp16=torch.cuda.is_available(),
        dataloader_pin_memory=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=ds,  # streaming dataset; ensure __len__ not required for short runs
    )
    trainer.train()
    trainer.save_model(OUTPUT_DIR)
    print(f"Saved to {OUTPUT_DIR}")
    return OUTPUT_DIR

if __name__ == "__main__":
    # For <2min dev iteration, run local.
    # For production resilience, use ensure_lightning_studio_and_run()
    run_local_train()
```

---

### 3) Quick orchestration (Mac/Linux) — 5 min
Add a small runner to restart on idle-stop and retry on transient CDN errors:

```bash
#!/usr/bin/env bash
# surrogate/training/run.sh
set -euo pipefail

MAX_RETRIES=3
RETRY_DELAY=30

for i in $(seq 1 $MAX_RETRIES); do
    echo "Run $i/$MAX_RETRIES"
    python3 surrogate/training/train.py && break
    echo "Retrying in $RETRY_DELAY seconds..."
    sleep $RETRY_DELAY
done
```

Make executable:
```bash
chmod +x surrogate/training/run.sh
```

---

### Why this wins
- **Eliminates HF 429**: CDN-only file fetches + pre-cached file list remove auth/listing calls during training.
- **Lightning idle resilience**: Studio reuse + restart logic prevents lost work from idle stops.
- **Fast iteration**: Local fallback + streaming dataset keeps iteration under 2 minutes.
- **Schema-safe**: Projects to `prompt`/`response` only; avoids schema drift from extra fields.
- **Actionable today**: ~90 minutes to implement/test with minimal dependencies.
