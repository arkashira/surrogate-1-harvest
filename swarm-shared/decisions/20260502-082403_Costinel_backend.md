# Costinel / backend

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate-limit blocks during dataset training.

### Implementation Plan
1. **Identify the dataset repository**: Determine the Hugging Face dataset repository that is being used for training.
2. **Pre-list file paths**: Make a single API call to `list_repo_tree(path, recursive=False)` to get the list of file paths for the dataset.
3. **Save file paths to JSON**: Save the list of file paths to a JSON file.
4. **Embed JSON in training script**: Modify the training script to read the file paths from the JSON file and use the CDN URLs to download the files.
5. **Use CDN URLs for training**: Update the training script to use the CDN URLs to download the files instead of making API calls.

### Code Snippets
```python
import json
import requests

# Pre-list file paths
repo_id = "dataset/repo"
path = "path/to/dataset"
response = requests.get(f"https://huggingface.co/api/repo/{repo_id}/tree/{path}", params={"recursive": False})
file_paths = response.json()

# Save file paths to JSON
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# Embed JSON in training script
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

# Use CDN URLs for training
cdn_url = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
for file_path in file_paths:
    file_url = cdn_url.format(repo=repo_id, path=file_path)
    # Download file using CDN URL
    response = requests.get(file_url)
    with open(file_path, "wb") as f:
        f.write(response.content)
```
### Benefits
This improvement will allow the training script to bypass the API rate-limit blocks and download the dataset files directly from the CDN, reducing the training time and improving the overall efficiency of the system.
