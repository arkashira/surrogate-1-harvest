# airship / discovery

### Highest-Value Incremental Improvement
The highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate limits when downloading dataset files. This can be achieved by modifying the `train.py` script to download files directly from the HF CDN instead of using the HF API.

### Implementation Plan
1. **Identify the dataset repository**: Determine the repository from which the dataset files are being downloaded.
2. **Get the list of file paths**: Use the HF API to get the list of file paths for the dataset repository. This can be done using the `list_repo_tree` method.
3. **Save the list of file paths to a JSON file**: Save the list of file paths to a JSON file that can be embedded in the `train.py` script.
4. **Modify the `train.py` script**: Modify the `train.py` script to download files directly from the HF CDN using the list of file paths saved in the JSON file.

### Code Snippets
```python
import json
import requests

# Get the list of file paths for the dataset repository
repo_id = "dataset/repo"
file_paths = []
response = requests.get(f"https://huggingface.co/{repo_id}/tree/main")
for file in response.json():
    file_paths.append(file["path"])

# Save the list of file paths to a JSON file
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# Modify the train.py script to download files directly from the HF CDN
import json

# Load the list of file paths from the JSON file
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

# Download files directly from the HF CDN
for file_path in file_paths:
    url = f"https://huggingface.co/{repo_id}/resolve/main/{file_path}"
    response = requests.get(url)
    with open(file_path, "wb") as f:
        f.write(response.content)
```
This implementation plan and code snippets provide a concrete solution to implement the HF CDN Bypass pattern and avoid API rate limits when downloading dataset files.
