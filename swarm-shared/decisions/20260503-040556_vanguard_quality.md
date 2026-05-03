# vanguard / quality

# Final Synthesis (single, actionable plan)

## 1. Root-cause summary (merged)
- **No content-addressed manifest** → runtime repo scans trigger HF API 429 and non-reproducible epochs.
- **Schema drift** (`enriched/` mixed columns) → `pyarrow.CastError` in surrogate-1 training.
- **HF API in data path** (`load_dataset`, `list_repo_files`, `streaming=True`) → guaranteed rate-limits under load.
- **Studio lifecycle mismanagement** → recreation on every run burns quota; idle-stop kills jobs non-deterministically.

## 2. One-shot fix: deterministic manifest + CDN-only loader + Studio reuse

### 2.1 Manifest generator (single API call, content-addressed)
`/opt/axentx/vanguard/ingest/manifest.py`
- Lists **top-level date folder only** (non-recursive) → one `list_repo_tree` call.
- Produces `batches/mirror-merged/{YYYY-MM-DD}/filelist.json`.
- Each entry:  
  ```json
  {
    "cdn_url": "https://huggingface.co/datasets/datasets%2Fmirror-merged/resolve/main/...",
    "prompt_col": "prompt",
    "response_col": "response",
    "sha256": "<12-char slug>"
  }
  ```
- **Correctness**: rejects non-parquet; uses CDN URL (no auth header) to bypass HF API limits.

```bash
chmod +x /opt/axentx/vanguard/ingest/manifest.py
```

### 2.2 CDN-only dataset loader (zero HF API)
`/opt/axentx/vanguard/train/data.py`
- `CdnParquetDataset(manifest_path)` reads only from CDN URLs in manifest.
- Projects schema to `{prompt,response}` at parse time (tolerates extra columns).
- Uses `requests.get(cdn_url)` + `pyarrow.parquet.read_table` → no HF client, no 429 from training.
- Returns one sample per parquet row; DataLoader batches across files.

### 2.3 Training orchestrator with Studio reuse
`/opt/axentx/vanguard/train/run.py`
- **Reuse policy**: pick existing running Studio by name; start only if stopped.
- **Fail-fast**: abort if manifest missing (forces explicit regeneration).
- **Deterministic args**: passes manifest path and epochs to train loop.
- **No recreation**: prevents quota burn and idle-stop losses.

```bash
chmod +x /opt/axentx/vanguard/train/run.py
```

### 2.4 Minimal train loop stub
`/opt/axentx/vanguard/train/train_loop.py`
- Accepts `--manifest` and `--epochs`.
- Instantiates `CdnParquetDataset` + `DataLoader`.
- Replace model/training logic as needed; data path is now CDN-only and schema-clean.

## 3. Execution order (concrete)

```bash
# 1) Generate manifest once per day (or per snapshot)
python /opt/axentx/vanguard/ingest/manifest.py

# 2) Verify manifest exists
ls -l /opt/axentx/vanguard/batches/mirror-merged/$(date +%F)/filelist.json

# 3) Launch/reuse Studio and start training
python /opt/axentx/vanguard/train/run.py
```

## 4. Why this resolves contradictions
- **Manifest vs no-manifest**: manifest is mandatory; training refuses to run without it (reproducibility + rate-limit avoidance).
- **HF API vs CDN**: loader uses CDN exclusively; HF API limited to one-time manifest generation (top-level only).
- **Schema drift**: projection to `{prompt,response}` happens at manifest build and load time, eliminating `CastError`.
- **Studio recreation vs reuse**: orchestrator picks running Studio first; never recreates on every run (quota-safe).
