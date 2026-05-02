# surrogate-1 / discovery

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not leverage the Hugging Face CDN for rate-limit-free downloads.
* The existing implementation does not handle the case where a Lightning Studio instance is idle or stopped, causing training to fail.

**Proposed change**: Implement a robust Hugging Face API rate limit handling mechanism, leveraging the CDN for rate-limit-free downloads, and ensure Lightning Studio instance reuse.

**Implementation**

1. **Rate Limit Handling**:
   - Update `train.py` to use the Hugging Face CDN for rate-limit-free downloads by replacing `hf_hub_download` with `requests.get` to download files directly from the CDN.
   - Implement a retry mechanism with exponential backoff to handle API rate limit errors (429).
   - Use the `list_repo_tree` API to fetch file paths in a single call, and save the list to a JSON file for future use.

2. **Lightning Studio Reuse**:
   - Update `launch_lightning.py` to check if a suitable Lightning Studio instance is available before creating a new one.
   - Use the `Teamspace.studios` list to find a running instance with the desired configuration, and reuse it if available.

3. **CDN-based Data Loading**:
   - Update `data_loader.py` to use the Hugging Face CDN for rate-limit-free downloads by replacing `hf_hub_download` with `requests.get` to download files directly from the CDN.

**Implementation Code**

```python
# train.py
import requests
import json

# ...

def download_file_from_cdn(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    return response.content

def load_data():
    # ...
    file_paths = json.load(open("file_paths.json"))
    for file_path in file_paths:
        file_content = download_file_from_cdn(repo, file_path)
        # ...
```

```python
# launch_lightning.py
import lightning as L

# ...

def launch_lightning():
    # ...
    for s in Teamspace.studios:
        if s.name == "my_studio" and s.status == "Running":
            studio = s
            break
    if studio:
        # Reuse the existing studio instance
        return studio
    # ...
```

```python
# data_loader.py
import requests

# ...

def load_data():
    # ...
    file_paths = json.load(open("file_paths.json"))
    for file_path in file_paths:
        file_content = download_file_from_cdn(repo, file_path)
        # ...
```

**Verification**

1. Run `train.py` with the updated rate limit handling mechanism and verify that it can download files from the Hugging Face CDN without hitting rate limits.
2. Run `launch_lightning.py` and verify that it reuses an existing Lightning Studio instance if available.
3. Verify that the `data_loader.py` uses the Hugging Face CDN for rate-limit-free downloads.
