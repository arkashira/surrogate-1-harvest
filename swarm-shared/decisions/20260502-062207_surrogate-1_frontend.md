# surrogate-1 / frontend

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The frontend implementation does not leverage the HF CDN bypass to download public dataset files, relying on the API which is rate-limited.
* The project does not have a clear strategy for handling file paths and caching to avoid repeated API calls.

**Proposed change**
* Implement HF CDN bypass for public dataset files in the frontend code.

**Implementation**
1. In `train.py`, replace the `list_repo_files` API call with a single API call to `list_repo_tree` for one date folder.
2. Save the list of file paths to a JSON file.
3. Embed the JSON file in the frontend code to avoid repeated API calls.
4. Modify the frontend code to download public dataset files from the HF CDN using the file paths from the JSON file.

**Implementation Code**
```python
# train.py
import json
import os

# ...

# Replace list_repo_files with list_repo_tree
file_paths = hf_api.list_repo_tree(path="path/to/date/folder", recursive=False)

# Save file paths to JSON file
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# ...

# In frontend code
import json

# Load file paths from JSON file
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

# Download public dataset files from HF CDN
for file_path in file_paths:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    # Download file using requests or other library
```

**Verification**
1. Check that the frontend code is downloading public dataset files from the HF CDN instead of relying on the API.
2. Verify that the file paths are being cached correctly and not repeated API calls are being made.
3. Test that the training process completes successfully without hitting the API rate limit.
