# surrogate-1 / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information. Both candidates propose a similar approach to eliminate HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline. The key steps involve:

1. **Replacing recursive authenticated fetches** with a single non-recursive `list_repo_tree` per date folder.
2. **Deterministic shard routing** to ensure efficient data processing.
3. **CDN-only fetches** to reduce the load on the API and improve performance.

The proposed changes include modifying `bin/dataset-enrich.sh` to implement these steps, keeping `lib/dedup.py` unchanged as the central md5 store, and updating `.github/workflows/ingest.yml` to pass the necessary parameters to the worker script.

**Synthesized Final Answer:**

To address the HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline, implement the following changes:

### Changes

1. **`bin/dataset-enrich.sh`**:
	* Accept `DATE_FOLDER` and `SHARD_ID`/`TOTAL_SHARDS` as environment parameters.
	* Use `huggingface_hub` CLI or a small Python helper to call `list_repo_tree` once per date folder and save the file list to `/tmp/filelist.json`.
	* Implement deterministic shard assignment using `hash(slug) % TOTAL_SHARDS == SHARD_ID`.
	* Fetch files via CDN (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `curl`/`requests` (no auth header).
	* Stream-parse each file, project to `{prompt, response}`, compute md5, and emit JSONL.
	* Upload shard output to `batches/public-merged/<date>/shard<SHARD_ID>-<HHMMSS>.jsonl`.
2. **`lib/dedup.py`**:
	* Keep unchanged as the central md5 store.
3. **`.github/workflows/ingest.yml`**:
	* Pass matrix `shard_id`/`total_shards` and computed `date_folder` to the worker script.
	* Keep the 16-shard matrix; no other changes.

**Code Snippets:**

The provided code snippets for `bin/dataset-enrich.sh` and `lib/dedup.py` can be used as a starting point. However, to improve the solution, consider the following:

* Use a more efficient data processing library, such as `pyarrow` or `dask`, to handle large datasets.
* Implement error handling and logging mechanisms to ensure robustness and debuggability.
* Consider using a more advanced deduplication strategy, such as using a Bloom filter or a more efficient hash function.

By implementing these changes, you can effectively eliminate the HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline, ensuring a more efficient and scalable data processing workflow.
