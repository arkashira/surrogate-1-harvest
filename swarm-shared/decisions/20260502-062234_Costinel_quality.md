# Costinel / quality

**Task:** Implement HF CDN Bypass for Surrogate-1 Training Pipeline

**Highest-value incremental improvement:** Bypass HF API rate limit by downloading public dataset files from CDN

**Implementation Plan:**

1. **Update `train.py` to use CDN downloads**: Modify the `train.py` script to download files from the HF CDN instead of using the API. Use the `hf_hub_download` function with the `cdn` parameter set to `True`.
2. **Pre-list file paths once and save to JSON**: Use a single API call from the Mac (after the rate-limit window clears) to `list_repo_tree(path, recursive=False)` for one date folder. Save the list of file paths to a JSON file.
3. **Embed file list in `train.py`**: Read the JSON file containing the list of file paths and embed it in the `train.py` script.

**Code Snippets:**

```python
import os
import json
from huggingface_hub import hf_hub_download

# Pre-list file paths and save to JSON
def pre_list_file_paths(repo, path, date_folder):
    api_url = f"https://api.huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(api_url)
    file_paths = response.json()["files"]
    with open("file_paths.json", "w") as f:
        json.dump(file_paths, f)

# Update train.py to use CDN downloads
def train():
    with open("file_paths.json", "r") as f:
        file_paths = json.load(f)
    for file_path in file_paths:
        hf_hub_download(repo="your-repo", path=file_path, cdn=True)
```

**Expected Outcome:** The Surrogate-1 Training Pipeline will bypass the HF API rate limit by downloading public dataset files from the CDN, reducing the risk of rate limit errors and improving training efficiency.
