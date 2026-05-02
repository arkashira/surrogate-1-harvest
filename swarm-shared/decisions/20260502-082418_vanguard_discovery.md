# vanguard / discovery

### 1. **Diagnosis**
* The Vanguard project lacks a comprehensive solution to handle HF API rate limits, which can block dataset training and hinder the project's progress.
* The current implementation does not utilize the HF CDN bypass strategy, which can download public dataset files without hitting the API rate limit.
* The project's training pipeline is not optimized for performance and reliability, leading to potential errors and inefficiencies.
* The lack of a robust solution for handling HF API rate limits and optimizing the training pipeline can significantly impact the project's overall performance and progress.
* The project's current architecture does not fully leverage the capabilities of the HF CDN and Lightning Studio, leading to potential bottlenecks and inefficiencies.

### 2. **Proposed change**
The proposed change is to implement the HF CDN bypass strategy in the training pipeline to download public dataset files without hitting the API rate limit. This will involve modifying the `train.py` script to use the HF CDN URL to download dataset files, and implementing a caching mechanism to store the downloaded files.

### 3. **Implementation**
To implement the HF CDN bypass strategy, the following steps can be taken:
1. Modify the `train.py` script to use the HF CDN URL to download dataset files. This can be done by replacing the `load_dataset` function with a custom function that downloads the dataset files from the HF CDN URL.
2. Implement a caching mechanism to store the downloaded dataset files. This can be done using a library like `joblib` or `cachecontrol`.
3. Update the `train.py` script to use the cached dataset files instead of re-downloading them from the HF CDN URL.

Example code snippet:
```python
import os
import requests
from joblib import Memory

# Define the HF CDN URL and the dataset file path
hf_cdn_url = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
dataset_file_path = "path/to/dataset/file"

# Define the caching mechanism
memory = Memory(location="/tmp/cache", verbose=0)

# Define the custom function to download dataset files from the HF CDN URL
@memory.cache
def download_dataset_file(repo, path):
    url = hf_cdn_url.format(repo=repo, path=path)
    response = requests.get(url)
    with open(dataset_file_path, "wb") as f:
        f.write(response.content)

# Update the train.py script to use the cached dataset files
def train():
    # Download the dataset file from the HF CDN URL
    download_dataset_file("repo", "path/to/dataset/file")

    # Load the dataset file from the cache
    with open(dataset_file_path, "rb") as f:
        dataset = pickle.load(f)

    # Train the model using the dataset
    # ...
```
### 4. **Verification**
To verify that the HF CDN bypass strategy is working correctly, the following steps can be taken:
1. Run the `train.py` script and verify that the dataset files are being downloaded from the HF CDN URL.
2. Check the cache directory to verify that the dataset files are being cached correctly.
3. Verify that the model is being trained correctly using the cached dataset files.
4. Monitor the API rate limit and verify that it is not being exceeded.

Example verification code snippet:
```python
import os
import requests

# Define the HF CDN URL and the dataset file path
hf_cdn_url = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
dataset_file_path = "path/to/dataset/file"

# Verify that the dataset file is being downloaded from the HF CDN URL
response = requests.get(hf_cdn_url.format(repo="repo", path="path/to/dataset/file"))
assert response.status_code == 200

# Verify that the dataset file is being cached correctly
assert os.path.exists(dataset_file_path)

# Verify that the model is being trained correctly using the cached dataset files
# ...
```
