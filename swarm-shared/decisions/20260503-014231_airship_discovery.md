# airship / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided candidates.

**Candidate 1** proposes the following steps to eliminate HF API 429s and Lightning idle-timeout training failures:

1. Pre-list dataset files using a script (`list_hf_dataset.py`) to avoid API pagination and 429 errors.
2. Implement a CDN-only dataset loader (`cdn_dataset.py`) that uses the pre-listed manifest to load parquet files via the HF CDN, avoiding API calls during training.
3. Modify the Lightning training script (`train_cdn.py`) to use the CDN-only dataset loader and implement idle-safe studio reuse.

**Candidate 2** proposes a similar approach, focusing on:

1. Switching to CDN-only dataset fetches with a pre-listed file manifest.
2. Implementing studio reuse and auto-restart to prevent idle-timeout failures.

After analyzing both candidates, the strongest insights can be combined into a single, final answer:

**Final Answer**

To eliminate HF API 429s and Lightning idle-timeout training failures, implement the following steps:

1. **Pre-list dataset files**: Use a script (e.g., `list_hf_dataset.py`) to pre-list dataset files and create a manifest file, avoiding API pagination and 429 errors.
2. **CDN-only dataset loader**: Implement a CDN-only dataset loader (e.g., `cdn_dataset.py`) that uses the pre-listed manifest to load parquet files via the HF CDN, avoiding API calls during training.
3. **Idle-safe studio reuse**: Modify the Lightning training script (e.g., `train_cdn.py`) to use the CDN-only dataset loader and implement idle-safe studio reuse with auto-restart, ensuring that the studio is running before training and restarting it if necessary.

By following these steps, you can effectively eliminate HF API 429s and Lightning idle-timeout training failures, ensuring a more reliable and efficient training process.

**Code**

The code provided in Candidate 1 can be used as a starting point, with some modifications to incorporate the insights from Candidate 2. The key files are:

* `list_hf_dataset.py`: Pre-list dataset files and create a manifest file.
* `cdn_dataset.py`: Implement a CDN-only dataset loader using the pre-listed manifest.
* `train_cdn.py`: Modify the Lightning training script to use the CDN-only dataset loader and implement idle-safe studio reuse with auto-restart.

**Example Use Case**

To use this solution, follow these steps:

1. Run the `list_hf_dataset.py` script to pre-list dataset files and create a manifest file.
2. Modify the `train_cdn.py` script to use the CDN-only dataset loader and implement idle-safe studio reuse with auto-restart.
3. Run the `train_cdn.py` script to start training with the CDN-only dataset loader and idle-safe studio reuse.

By following these steps, you can ensure a more reliable and efficient training process, eliminating HF API 429s and Lightning idle-timeout training failures.
