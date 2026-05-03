# surrogate-1 / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and resolve any contradictions in favor of correctness and concrete actionability.

The primary goal here is to implement a solution that allows for the efficient and deterministic loading of data for training, specifically by utilizing a Mac-side tool to generate a `file_manifest.json` for a Hugging Face dataset. This manifest contains CDN URLs and metadata, enabling zero-API fetches during data loading, thus bypassing the need for repeated API calls and preventing 429 errors during long training runs.

### Synthesized Implementation Plan

1. **Create `tools/snapshot_manifest.py`**:
   - This script will accept parameters such as `--repo`, `--date`, and an optional `--out` for specifying the output file path.
   - It makes a single `list_repo_tree` call to the Hugging Face API for the specified repository under a given date partition.
   - The script generates a `file_manifest.json` containing necessary metadata (e.g., `generated_at`, `snapshot_id`, `repo`, `date`, `files` with `path`, `size`, `etag`, `cdn_url`, and a `shard_map` for deterministic shard assignment).
   - The `cdn_url` is constructed using a template to directly access files via the CDN, reducing the need for API calls during training.

2. **Implement `tools/verify_manifest.py`**:
   - This lightweight tool validates the schema of the generated `file_manifest.json`.
   - Optionally, it can perform a HEAD check on the CDN URLs listed in the manifest to ensure they are reachable, with a configurable timeout.

3. **Update Training Launcher**:
   - Modify the training script (e.g., `train.py` or a Lightning script) to read the `file_manifest.json`.
   - Implement an `IterableDataset` that downloads files directly from the CDN URLs provided in the manifest, utilizing streaming and `Range` requests for efficiency.
   - Ensure deterministic shard selection based on the `shard_id` and `world_size` to facilitate parallel training.

4. **Add to Repository**:
   - Include the necessary dependencies in `requirements-dev.txt`.
   - Place the `tools/` directory with `__main__` entry points for `snapshot_manifest.py` and `verify_manifest.py`.
   - Document the usage and purpose of these tools in `tools/README.md`.

5. **Smoke Test**:
   - Run `snapshot_manifest.py` for a recent date partition.
   - Verify that the generated manifest contains CDN URLs and is correctly formatted.
   - Perform a mini-training run with a small sample to confirm that data is loaded correctly from the CDN without making API calls.

### Estimated Time and Actionability

The estimated time for implementing these steps is approximately 120 minutes, considering coding, testing, and documentation. This plan is actionable because it:

- Directly addresses the need to reduce API calls during training.
- Provides a clear, step-by-step approach to implementation.
- Includes tools for validation and verification of the manifest and CDN URLs.
- Enhances the efficiency and determinism of data loading for training.

By following this synthesized plan, developers can efficiently implement a solution that bypasses the Hugging Face API for data loading during training, reducing the likelihood of 429 errors and improving overall training efficiency.
