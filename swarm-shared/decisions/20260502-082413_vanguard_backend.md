# vanguard / backend

### 1. **Diagnosis**
* The Vanguard project lacks a comprehensive solution to handle HF API rate limits, which can block dataset training and hinder the project's progress.
* The current implementation does not utilize the HF CDN bypass strategy, which can download public dataset files without being subject to the same rate limits as the HF API.
* The project's training pipeline may be interrupted by rate limit errors, causing delays and inefficiencies in the development process.
* The existing codebase may not be optimized for handling large datasets and high-volume data ingestion, leading to potential performance issues.
* There is a need to implement a more robust and efficient data ingestion pipeline that can handle rate limits and large datasets effectively.

### 2. **Proposed change**
The proposed change involves modifying the `train.py` script to utilize the HF CDN bypass strategy for downloading public dataset files. This change will be implemented in the `/opt/axentx/vanguard/train.py` file, specifically in the data loading section.

### 3. **Implementation**
To implement the HF CDN bypass strategy, we will modify the `train.py` script as follows:
```python
import json
import requests

# Load the list of dataset files from a JSON file
with open('dataset_files.json', 'r') as f:
    dataset_files = json.load(f)

# Define the HF CDN URL template
cdn_url_template = 'https://huggingface.co/datasets/{repo}/resolve/main/{path}'

# Download the dataset files using the HF CDN bypass strategy
for file in dataset_files:
    repo = file['repo']
    path = file['path']
    cdn_url = cdn_url_template.format(repo=repo, path=path)
    response = requests.get(cdn_url)
    with open(path, 'wb') as f:
        f.write(response.content)
```
We will also create a `dataset_files.json` file that contains the list of dataset files to be downloaded. This file will be generated using the `list_repo_tree` API call, which will be made from the Mac orchestration script.

### 4. **Verification**
To verify that the HF CDN bypass strategy is working correctly, we can check the following:

* The `dataset_files.json` file is generated correctly and contains the list of dataset files to be downloaded.
* The `train.py` script downloads the dataset files successfully using the HF CDN bypass strategy.
* The training pipeline completes without any rate limit errors or interruptions.
* The performance of the data ingestion pipeline is improved, and the project's development process is accelerated.

We can verify these points by running the `train.py` script and monitoring its output, as well as checking the project's logs and metrics to ensure that the data ingestion pipeline is working efficiently and effectively.
