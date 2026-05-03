# surrogate-1 / backend

**Synthesized Implementation Plan**

The highest-value change is to add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a single HF API call, emits `file_manifest.json` with CDN URLs and integrity metadata, and updates the training script to consume the manifest, ensuring zero HF API calls during data load (CDN-only).

**Steps (120 min total)**

1. **Create tools/snapshot_manifest.py** (30 min)
	* Single `list_repo_tree(path, recursive=False)` call for one date folder
	* Emit `file_manifest.json`: `{date, repo, files: [{path, cdn_url, size, etag}]}` with deterministic sort for reproducibility
2. **Update train.py** (40 min)
	* Accept `--manifest file_manifest.json`
	* Data loader downloads via `requests.get(cdn_url, stream=True)` with retries and integrity checks
	* Parse parquet → project `{prompt, response}` only
	* Zero HF API calls during training loop
3. **Add util/download.py** (20 min)
	* CDN download with exponential backoff, integrity check (size/etag), and resume support
	* Respect HF CDN limits (parallelism=8)
4. **Update requirements.txt** (5 min)
	* Add `requests>=2.31`, `pyarrow`, `tqdm`
5. **Smoke test** (25 min)
	* Run snapshot against `axentx/surrogate-1-training-pairs` for a recent date
	* Run train.py with manifest on a small sample (10 files)
	* Verify no HF API traffic (check logs)

**Code**

The provided code for `tools/snapshot_manifest.py` and `train.py` is well-structured and effective. The `download_cdn_file` function in `train.py` handles retries, integrity checks, and resume support, ensuring reliable downloads from the CDN.

**Key Benefits**

1. **Eliminates HF API rate-limit risk**: By pre-listing the files in the manifest, we avoid making multiple HF API calls during training, reducing the risk of hitting rate limits (429s).
2. **Improves training efficiency**: CDN-only data loading reduces the overhead of API calls, making training faster and more efficient.
3. **Ensures data integrity**: The manifest includes integrity metadata (size, etag), and the `download_cdn_file` function performs integrity checks, ensuring that the downloaded files are correct and complete.

**Conclusion**

The synthesized implementation plan combines the strongest insights from the candidate proposals, resolving contradictions in favor of correctness and concrete actionability. The resulting plan ensures a reliable, efficient, and scalable solution for training surrogate-1 models using CDN-only data loading, eliminating HF API rate-limit risks and improving training efficiency.
