# surrogate-1 / backend

Candidate 3:
## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Add deterministic pre-flight file listing + CDN-only ingestion path to eliminate HF API rate limits during training and make shard workers resilient to 429s.

### What we’ll change
1. Add `bin/list-files.sh` — runs once on Mac (or after rate-limit window) to snapshot a date folder’s file list via `list_repo_tree(recursive=False)` and emit `file-list.json`.
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` path; if provided, workers read paths from the list and download via CDN (`resolve/main/...`) instead of using `load_dataset`/`datasets` streaming API.
3. Embed the same file-list into training scripts later (Lightning Studio) so training does zero HF API calls during data load.
4. Keep existing streaming path as fallback when no file list is provided.

### Why this is highest value
- Eliminates 429s during ingestion and training (CDN tier has much higher limits).
- Single API call per date folder → safe under 1000 req/5min cap.
- Enables parallel shard workers without per-shard API pressure.
- Fits existing layout; no schema changes; <2h to ship.

---

## Files to create/modify

### 1) bin/list-files.sh
```b
