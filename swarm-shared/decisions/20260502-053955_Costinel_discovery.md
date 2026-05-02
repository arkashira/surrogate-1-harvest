# Costinel / discovery

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate limits when downloading dataset files.

### Implementation Plan
1. **Identify dataset files**: Pre-list file paths once using a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
2. **Save file list to JSON**: Embed the list in the training script `train.py`.
3. **Use CDN-only fetches**: Modify the training script to download dataset files from the CDN using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern, bypassing the API rate limit.

### Code Snippets
```python
import json
import requests

# Pre-list file paths using a single API call
def get_file_list(repo, path):
    response = requests.get(f"https://huggingface.co/datasets/{repo}/tree/main/{path}")
    file_list = response.json()
    return file_list

# Save file list to JSON
file_list = get_file_list("axentx", "datasets")
with open("file_list.json", "w") as f:
    json.dump(file_list, f)

# Use CDN-only fetches in train.py
import json

with open("file_list.json", "r") as f:
    file_list = json.load(f)

for file in file_list:
    file_url = f"https://huggingface.co/datasets/axentx/resolve/main/{file}"
    response = requests.get(file_url)
    # Process the file
```
### Expected Outcome
By implementing the HF CDN Bypass pattern, we can avoid API rate limits when downloading dataset files, reducing the likelihood of errors and improving the overall efficiency of the training process. This incremental improvement can be shipped in <2h, providing a quick win for the Costinel project.
