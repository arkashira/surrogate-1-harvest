# vanguard / quality

### 1. **Diagnosis**
* The Vanguard project lacks a comprehensive solution to handle HF API rate limits, which can block dataset training and hinder the project's progress.
* The current implementation does not utilize the HF CDN bypass strategy, which can download public dataset files without being subject to the same rate limits as the HF API.
* The project's training scripts do not pre-list file paths once and embed them in the training script, leading to unnecessary API calls during data loading.
* The project does not reuse existing Lightning Studios, resulting in wasted quota and potential idle timeouts.
* The project's Surrogate-1 training pipeline may be affected by pyarrow CastError on HF datasets with mixed schema files.

### 2. **Proposed change**
The proposed change will focus on implementing the HF CDN bypass strategy and pre-listing file paths in the training script. This will involve modifying the `train.py` script to use the HF CDN bypass strategy and embedding the pre-listed file paths in the script.

### 3. **Implementation**
To implement the proposed change, follow these steps:
1. **Pre-list file paths**: Make a single API call to `list_repo_tree(path, recursive=False)` for one date folder and save the list to a JSON file.
2. **Modify train.py**: Embed the pre-listed file paths in the `train.py` script and use the HF CDN bypass strategy to download public dataset files.
3. **Use CDN-only fetches**: Modify the `train.py` script to use CDN-only fetches with zero API calls during data loading.

Example code snippet:
```python
import json
import requests

# Pre-list file paths
repo_tree = requests.get(f"https://huggingface.co/datasets/{repo}/tree/main").json()
file_paths = [file["path"] for file in repo_tree["files"]]
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# Modify train.py
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

# Use CDN-only fetches
for file_path in file_paths:
    file_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    response = requests.get(file_url)
    # Process the file
```
### 4. **Verification**
To verify that the proposed change works, follow these steps:
1. **Run the modified train.py script**: Run the modified `train.py` script and verify that it uses the HF CDN bypass strategy and pre-listed file paths.
2. **Check API calls**: Verify that the script makes zero API calls during data loading.
3. **Check training progress**: Verify that the training process completes successfully and that the model is trained correctly.
4. **Monitor quota usage**: Monitor the quota usage and verify that it is reduced due to the reuse of existing Lightning Studios.
