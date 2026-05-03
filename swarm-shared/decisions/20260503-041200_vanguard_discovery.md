# vanguard / discovery

## Final Synthesis (single, correct, actionable)

**Core problem**: dataset loads are not content-addressed, so training and UI both hit the HF API at runtime (429s, non-reproducible epochs, no shareable snapshots).  
**Resolution**: build one deterministic, content-addressed manifest per snapshot and make training/UI consume only CDN URLs with zero authenticated API calls.

---

## 1. Manifest design (content-addressed, minimal, canonical)

- **One manifest per snapshot**: `snapshot-{date}-{hash}.json`
  - `hash = SHA256(sorted(file_paths + row_counts + schema_digest))[:16]`
  - Top-level fields: `repo`, `date`, `created_at`, `file_list_hash`, `total_files`, `total_rows`, `projection`, `files[]`
- **Per-file record** (in manifest and optional `.jsonl`):
  - `path`, `size`, `num_rows`, `prompt_col`, `response_col`, `cdn_url`
- **Projection enforcement**:
  - Require `prompt`/`response` (case-insensitive match).
  - If extra columns exist, drop them at read time (never persist altered parquet).
- **CDN-only URLs**:
  - `https://huggingface.co/datasets/{repo}/resolve/main/{path}`

---

## 2. Implementation (single script + training change)

### Script: `/opt/axentx/vanguard/scripts/build_snapshot_manifest.py`

- Runs on Mac/CI after `dataset-mirror` writes a dated folder.
- **One `list_repo_tree` call** for the date folder (recursive=False) + one optional deeper scan if parquet not at top.
- **Streaming projection**:
  - Use `pyarrow.parquet.ParquetFile` metadata for row counts (no full read).
  - Read only `prompt`/`response` columns for schema validation (small batch).
- **Content-addressing**:
  - Hash over sorted file paths + row counts + schema names.
- **Outputs**:
  - `data/manifests/snapshot-{date}-{hash}.json`
  - `data/manifests/snapshot-{date}-{hash}.jsonl` (optional line-per-file for fast streaming)
- **No side effects**:
  - Do not rewrite parquet; do not push automatically by default (opt-in flag).
  - Clean up temporary downloads immediately.

### Wrapper (optional)

```bash
# /opt/axentx/vanguard/scripts/build_snapshot_manifest.sh
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$REPO_ROOT/scripts/build_snapshot_manifest.py" "$@"
```

### Training change: `/opt/axentx/vanguard/train.py` (or `train_cdn.py`)

- Add CLI arg: `--manifest PATH`
- Load manifest JSON; iterate `files` and fetch via `cdn_url` (or `path` + CDN base).
- Use streaming `pyarrow.parquet.ParquetDataset` or HF `datasets` with `data_files=[cdn_url]` (or direct fsspec `http` reads) — **no `list_repo_tree`/`load_dataset` API calls**.
- Cache downloaded files locally if desired (e.g., `~/.cache/axentx/`), but never require auth.

---

## 3. Correctness choices (where candidates diverged)

- **Do not rewrite parquet** to `enriched/` or elsewhere — keep originals immutable; project at read time.
- **Do not rely on runtime file listing** in training or UI — manifest is the source of truth.
- **Do not authenticate during training** — use only CDN `resolve/main/...` URLs.
- **Do not auto-push** by default — produce local manifest for review/commit; allow `--push` opt-in.
- **Deterministic hash inputs**: file paths, row counts, schema names (prompt/response). Do not include transient metadata or file content checksums (avoids full-file reads).

---

## 4. Concrete rollout steps

1. Add `build_snapshot_manifest.py` and optional wrapper to repo.
2. After next `dataset-mirror` run, execute:
   ```bash
   ./scripts/build_snapshot_manifest.sh 2026-05-03
   ```
3. Commit resulting `data/manifests/snapshot-2026-05-03-*.json`.
4. Update training launcher to accept `--manifest data/manifests/snapshot-2026-05-03-*.json` and use CDN URLs.
5. Update UI dataset picker to list available manifests and use the selected manifest’s `files` for display and download (CDN-only).
6. Verify zero authenticated HF API calls during training (e.g., monitor logs and rate-limit headers).
