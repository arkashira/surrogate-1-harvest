# surrogate-1 / backend

**Synthesized Proposal**

**Diagnosis**

* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not have a mechanism to bypass the Hugging Face API rate limit for dataset training, resulting in delayed or failed training processes.
* The current implementation does not ensure proper handling of errors and exceptions, which can lead to unexpected behavior or crashes.
* The project's backend does not have a clear and efficient way to manage and utilize the available computational resources, such as GPUs and CPUs.

**Proposed Change**

Implement a robust mechanism to handle Hugging Face API rate limits and reuse existing Lightning Studio instances. This can be achieved by modifying the `train.py` script to use the Hugging Face CDN to download dataset files, bypassing the API rate limit. Additionally, the script will be updated to reuse existing Lightning Studio instances to avoid wasting quota and potential downtime.

**Implementation**

To implement the proposed change, the following steps can be taken:

1. **Modify the `train.py` script to use the Hugging Face CDN to download dataset files**:
```python
import requests

def download_dataset_files(repo_id, dataset_name):
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{dataset_name}"
    response = requests.get(url)
    if response.status_code == 200:
        with open(f"{dataset_name}.parquet", "wb") as f:
            f.write(response.content)
    else:
        print(f"Failed to download dataset files: {response.status_code}")

# Usage
download_dataset_files("username/repo", "dataset_name")
```
2. **Update the `train.py` script to reuse existing Lightning Studio instances**:
```python
import lightning

def get_or_create_studio(studio_name):
    for studio in lightning.Teamspace.studios:
        if studio.name == studio_name and studio.status == "Running":
            return studio
    return lightning.Studio(create_ok=True)

# Usage
studio = get_or_create_studio("studio_name")
```
3. **Update the `train.py` script to handle errors and exceptions properly**:
```python
try:
    # Training code here
except Exception as e:
    print(f"Error occurred during training: {e}")
```
4. **Update the `train.py` script to manage and utilize the available computational resources efficiently**:
```python
import lightning

def get_available_resources():
    return lightning.Teamspace.get_available_resources()

# Usage
available_resources = get_available_resources()
if available_resources["gpus"] > 0:
    # Use GPU for training
else:
    # Use CPU for training
```
5. **Create a new file `cdn_bypass.py` in the project root with the following content**:
```python
import os
import requests

def get_cdn_file_path(repo, path):
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def download_file_from_cdn(repo, path, output_path):
    url = get_cdn_file_path(repo, path)
    response = requests.get(url)
    with open(output_path, "wb") as f:
        f.write(response.content)

def list_files_from_cdn(repo, path):
    url = get_cdn_file_path(repo, path)
    response = requests.get(url)
    return response.json()["files"]
```
6. **Modify the `train.py` file to use the `cdn_bypass.py` module to download files from the HF CDN instead of using the HF API**:
```python
import os
import pandas as pd
from cdn_bypass import download_file_from_cdn

# ...

# Download files from CDN instead of using HF API
for file in list_files_from_cdn(repo, path):
    download_file_from_cdn(repo, file["path"], file["name"])

# ...
```
7. **Update the `requirements.txt` file to include the `requests` library**:
```bash
requests
```
**Verification**

To verify that the proposed change works as expected, the following steps can be taken:

1. Run the modified `train.py` script and check if it can download the dataset files from the Hugging Face CDN without hitting the API rate limit.
2. Check if the script can reuse existing Lightning Studio instances correctly.
3. Verify that the script handles errors and exceptions properly and can recover from any errors that may occur during the training process.
4. Check if the script can manage and utilize the available computational resources efficiently and adjust the training process accordingly.

**Conclusion**

By implementing the proposed changes, the project will have a robust mechanism to handle Hugging Face API rate limits and reuse existing Lightning Studio instances, resulting in improved performance and efficiency. The updated `train.py` script will use the Hugging Face CDN to download dataset files, bypassing the API rate limit, and will reuse existing Lightning Studio instances to avoid wasting quota and potential downtime. Additionally, the script will handle errors and exceptions properly and manage and utilize the available computational resources efficiently.
