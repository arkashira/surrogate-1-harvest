# Costinel / backend

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid rate-limit blocks during dataset training.

### Implementation Plan
1. **Identify the dataset repository**: Determine the repository containing the dataset used for training.
2. **Pre-list file paths**: Make a single API call to `list_repo_tree(path, recursive=False)` for one date folder and save the list to a JSON file.
3. **Embed file list in training script**: Modify the training script to read the pre-listed file paths from the JSON file and use the CDN URLs to download the files.
4. **Update the training script**: Use the CDN URLs to download the files, bypassing the API rate-limit checks.

### Code Snippets
```python
import json
import requests

# Pre-list file paths
def pre_list_file_paths(repo_id, path):
    url = f"https://huggingface.co/api/v1/repo/{repo_id}/tree/{path}"
    response = requests.get(url)
    file_paths = response.json()
    with open("file_paths.json", "w") as f:
        json.dump(file_paths, f)

# Embed file list in training script
def load_file_paths():
    with open("file_paths.json", "r") as f:
        file_paths = json.load(f)
    return file_paths

# Update the training script
def train_model(file_paths):
    for file_path in file_paths:
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
        response = requests.get(url)
        # Process the file
        pass
```
### Example Use Case
```python
repo_id = "my-repo"
path = "my-path"
pre_list_file_paths(repo_id, path)
file_paths = load_file_paths()
train_model(file_paths)
```
This implementation plan and code snippets demonstrate how to apply the HF CDN Bypass pattern to avoid rate-limit blocks during dataset training. By pre-listing file paths and embedding them in the training script, we can bypass the API rate-limit checks and download the files directly from the CDN.
