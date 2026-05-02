# surrogate-1 / backend

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not have a mechanism to bypass the Hugging Face API rate limit for public dataset files, which can be downloaded directly from the CDN.
* The current implementation may not be taking full advantage of the CDN tier's higher rate limits.

**Proposed change**
* Implement a mechanism to bypass the Hugging Face API rate limit for public dataset files by downloading them directly from the CDN.

**Implementation**
```markdown
### Step 1: Modify the dataset download script to use the CDN

In `surrogate-1/train.py`, replace the `hf_hub_download` calls with `requests` calls to download the files directly from the CDN.

```python
import requests

# ...

# Download files from CDN
cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
response = requests.get(cdn_url, stream=True)
with open(file_path, "wb") as f:
    for chunk in response.iter_content(chunk_size=8192):
        f.write(chunk)
```

### Step 2: Update the file list generation script

In `surrogate-1/generate_file_list.py`, update the script to use the `list_repo_tree` API call with `recursive=False` to get the list of files in the repository, and then download the files directly from the CDN.

```python
import requests

# ...

# Get list of files in repository
response = requests.get(f"https://huggingface.co/api/v1/repos/{repo}/tree/{path}", params={"recursive": False})
files = response.json()["tree"]

# Download files from CDN
for file in files:
    cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file['path']}"
    response = requests.get(cdn_url, stream=True)
    with open(file["path"], "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
```

### Step 3: Update the training script to use the downloaded files

In `surrogate-1/train.py`, update the script to use the downloaded files instead of the original files.

```python
# ...
# Load downloaded files
files = []
for file in os.listdir("data"):
    files.append(os.path.join("data", file))

# ...
```

**Verification**
* Run the training script and verify that it completes successfully without hitting the Hugging Face API rate limit.
* Check the logs to ensure that the files are being downloaded directly from the CDN.
* Verify that the training results are accurate and consistent with the original implementation.
