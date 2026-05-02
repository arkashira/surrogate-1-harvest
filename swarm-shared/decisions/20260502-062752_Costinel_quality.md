# Costinel / quality

**Final Proposal: Implementing the HF CDN Bypass Pattern for Efficient Dataset Training**

**Highest-Value Incremental Improvement:**

The highest-value incremental improvement that can be shipped in <2h is to implement the HF CDN Bypass pattern to avoid rate-limit blocks during dataset training. This improvement will allow the training pipeline to download dataset files directly from the CDN, reducing the likelihood of rate-limit blocks and increasing the efficiency of the training pipeline.

**Implementation Plan:**

1. **Identify the dataset repository**: Determine the repository containing the dataset used for training.
2. **Pre-list file paths using `list_repo_tree`**: Use the `list_repo_tree` function to pre-list the file paths for the dataset. This will allow us to avoid making multiple API calls during training.
3. **Save file list to JSON**: Save the pre-listed file paths to a JSON file that can be embedded in the training script.
4. **Update the training script to use CDN downloads**: Modify the training script to download files from the HF CDN instead of using the HF API. This can be achieved by using the `hf_hub_download` function with the `cdn` parameter set to `True`.
5. **Embed file list in training script**: Embed the pre-listed file paths in the training script using a JSON import.

**Code Snippets:**

```python
# Pre-list file paths using list_repo_tree
import os
from transformers import HfFolder

def pre_list_file_paths(repo, path):
    file_paths = []
    for folder in HfFolder.list_repo_tree(repo, path, recursive=False):
        file_paths.extend(HfFolder.list_repo_tree(repo, folder, recursive=False))
    return file_paths

# Save file list to JSON
import json

def save_file_list(file_paths, filename):
    with open(filename, 'w') as f:
        json.dump(file_paths, f)

# Load file list from JSON
def load_file_list(filename):
    with open(filename, 'r') as f:
        return json.load(f)

# Update train.py to use CDN downloads
import json
import os
from transformers import HfFolder, hf_hub_download

def train():
    # Load pre-listed file paths from JSON
    file_list_filename = 'file_list.json'
    file_paths = load_file_list(file_list_filename)

    # Download files from CDN
    for file_path in file_paths:
        hf_hub_download(file_path, cdn=True)
```

**Benefits:**

Implementing the HF CDN Bypass pattern will allow the training pipeline to download dataset files directly from the CDN, avoiding the API rate limit and reducing the likelihood of rate-limit blocks. This improvement can be shipped in <2h and will have a significant impact on the training pipeline's efficiency.

**Commit Message:**

`Implement HF CDN Bypass pattern to avoid API rate-limit`

**Tags:**

* #huggingface
* #cdn
* #rate-limit-bypass
* #training
* #quality

**Action Items:**

1. Identify the dataset repository and pre-list the file paths using `list_repo_tree`.
2. Save the pre-listed file paths to a JSON file.
3. Update the training script to use CDN downloads and embed the pre-listed file paths.
4. Ship the updated training script and verify that it works correctly.
