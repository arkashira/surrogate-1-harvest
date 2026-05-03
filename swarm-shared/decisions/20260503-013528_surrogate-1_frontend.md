# surrogate-1 / frontend

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided candidates and extract the most valuable information.

**Common Goal**: Both candidates aim to eliminate the HF API rate-limit risk and Out-of-Memory (OOM) issues in the surrogate-1 ingestion pipeline.

**Key Insights**:

1. **Replace recursive authenticated fetches**: Both candidates suggest replacing recursive authenticated fetches with a single non-recursive `list_repo_tree` per date folder to reduce API calls and remove auth-check pressure during data loading.
2. **Deterministic shard routing**: Both candidates propose using deterministic shard routing to keep memory bounded per shard and ensure each shard processes only its assigned slice of files.
3. **CDN-only fetches**: Both candidates recommend using CDN-only fetches to bypass authentication and increase the limit on file downloads.
4. **Stream parsing and projection**: Candidate 1 suggests stream-parsing and projecting files on the fly to keep memory low and avoid accumulating full decoded batches in memory.
5. **Minimal code change**: Candidate 1 estimates a minimal code change of ~60-90 minutes, which is safe to ship.

**Concrete Changes**:

1. **Update `bin/dataset-enrich.sh`**: Candidate 1 provides a detailed example of how to update the `bin/dataset-enrich.sh` script to implement the proposed changes, including using `list_repo_tree`, computing deterministic shard assignment, and downloading files via CDN URLs.
2. **Add/Update Python runner module**: Candidate 1 also provides an example of how to update the Python runner module (`axentx/surrogate1/runner.py`) to accept `--cdn-only` and construct public CDN URLs, use streaming decompression and line-by-line projection, and integrate with a deduplication store.

**Final Answer**:

To address the HF API rate-limit risk and OOM issues in the surrogate-1 ingestion pipeline, we recommend implementing the following changes:

1. Replace recursive authenticated fetches with a single non-recursive `list_repo_tree` per date folder.
2. Use deterministic shard routing to keep memory bounded per shard.
3. Implement CDN-only fetches to bypass authentication and increase the limit on file downloads.
4. Stream-parse and project files on the fly to keep memory low.
5. Update the `bin/dataset-enrich.sh` script and the Python runner module (`axentx/surrogate1/runner.py`) to implement these changes.

These changes should be implemented within a 2-hour timeframe, as estimated by Candidate 1, and should provide a safe and effective solution to the identified issues.
