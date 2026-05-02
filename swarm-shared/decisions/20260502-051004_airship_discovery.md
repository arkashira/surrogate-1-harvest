# airship / discovery

### High-Value Incremental Improvement: Implementing HF CDN Bypass for Faster Data Ingestion
#### Problem Statement
The current data ingestion pipeline in Airship is limited by Hugging Face API rate limits, causing delays in data processing and training.

#### Solution Overview
Implement the HF CDN Bypass pattern to download dataset files directly from the Hugging Face CDN, bypassing the API rate limit. This involves pre-listing file paths, embedding them in the training script, and using the CDN to download files without API calls.

#### Implementation Plan

1. **Pre-list file paths**:
	* Run a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
	* Save the list of file paths to a JSON file.
2. **Embed file paths in training script**:
	* Modify the training script to read the JSON file containing the pre-listed file paths.
	* Use the CDN URLs to download the files directly, without making API calls.
3. **Update the data ingestion pipeline**:
	* Replace the existing API-based data ingestion with the CDN-based approach.
	* Ensure that the pipeline handles errors and exceptions properly.

#### Code Snippets

```python
import json
import requests

# Pre-list file paths
def pre_list_file_paths(repo, path):
    response = requests.get(f"https://huggingface.co/api/repo/{repo}/tree/{path}")
    file_paths = response.json()["files"]
    with open("file_paths.json", "w") as f:
        json.dump(file_paths, f)

# Embed file paths in training script
def load_file_paths():
    with open("file_paths.json", "r") as f:
        return json.load(f)

# Update the data ingestion pipeline
def ingest_data(file_paths):
    for file_path in file_paths:
        url = f"https://huggingface.co/{file_path}/resolve/main/{file_path}"
        response = requests.get(url)
        # Process the downloaded file
        pass
```

#### Example Use Case

```python
# Pre-list file paths
pre_list_file_paths("axentx/airship", "data")

# Load file paths in training script
file_paths = load_file_paths()

# Ingest data using the CDN
ingest_data(file_paths)
```

#### Benefits

* Bypasses Hugging Face API rate limits, allowing for faster data ingestion and training.
* Reduces the number of API calls, improving overall system efficiency.
* Enables the use of larger datasets and more frequent training iterations.
