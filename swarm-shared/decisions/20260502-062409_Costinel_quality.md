# Costinel / quality

**Final Proposal: Implement HF CDN Bypass for Dataset Training**

**Highest-value incremental improvement:** Bypass HF API rate limit by downloading public dataset files from CDN and caching file paths for one date folder to avoid repeated API calls.

**Implementation Plan:**

1. **Update `train.py` to use HF CDN Bypass**:
   - In `train.py`, replace `hf_hub_download` with `requests.get` to download files from the HF CDN directly.
   - Use the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL to download files without authentication.
   - Cache the list of file paths for one date folder in a JSON file, and load it in `train.py` to avoid repeated API calls.
   - Embed the file list in the training script using a single API call from Mac to `list_repo_tree(path, recursive=False)` for one date folder, save list to JSON, and embed in `train.py`.
2. **Update `dataset-mirror` to project to {prompt, response} only before upload**:
   - In `dataset-mirror`, project the dataset to {prompt, response} only before uploading it to the HF Space.
   - Move attribution to the filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`).
   - Remove the `source` and `ts` columns.
3. **Configure Lightning training to use CDN-only fetches with zero API calls during data load**:
   - Update the training script to use the cached file paths and CDN downloads instead of API calls.

**Code Snippets:**

```python
# train.py
import os
import json
import requests

# Load cached file paths for one date folder
def load_file_paths(repo, date):
    file_paths_json = f"{repo}/file_paths_{date}.json"
    with open(file_paths_json, "r") as f:
        file_paths = json.load(f)
    return file_paths

# Download files from HF CDN
def download_file(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    return response.content

# Update dataset loading to use CDN downloads
dataset = load_dataset('path/to/dataset', streaming=False, split='train')
for file in load_file_paths('path/to/repo', 'date'):
    dataset.add_file(file)
```

```python
# file_list.json
[
    "https://huggingface.co/datasets/{repo}/resolve/main/{path}",
    "https://huggingface.co/datasets/{repo}/resolve/main/{path}",
    ...
]
```

```python
# dataset-mirror
import pandas as pd

# Project dataset to {prompt, response} only before upload
def project_dataset(df):
    df = df[["prompt", "response"]]
    return df

# Move attribution to filename pattern
def move_attribution(df):
    df["filename"] = f"mirror-merged/{date}/{slug}.parquet"
    return df

# ...
```

**Commit Message:** `Implement HF CDN Bypass for Dataset Training`

**Tags:** `#training #api-strategy #file-list #cdn #rate-limit-bypass`

**Estimated Time:** 2 hours

This final proposal combines the strongest insights from both candidates, resolving contradictions in favor of correctness and concrete actionability. It provides a clear implementation plan, code snippets, and estimated time to complete the task.
