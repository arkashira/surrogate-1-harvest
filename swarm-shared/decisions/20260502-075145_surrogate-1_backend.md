# surrogate-1 / backend

### Synthesized Solution

The project lacks a robust implementation for handling Hugging Face API rate limits on the backend side, which can block dataset training. To address this issue, we will implement a mechanism to bypass the Hugging Face API rate limit for dataset training by using the Hugging Face CDN.

#### Proposed Change

The proposed change is to modify the `train.py` script to download dataset files directly from the Hugging Face CDN instead of using the Hugging Face API. This can be achieved by creating a JSON file `dataset_files.json` that contains the list of dataset files to download.

#### Implementation

To implement this change, we will follow these steps:

1. **Create a JSON file `dataset_files.json`**: This file will contain the list of dataset files to download. We can generate this file by running a script that uses the Hugging Face API to list the files in the repository, but only once, and then saves the list to the JSON file.
2. **Modify the `train.py` script**: We will modify the `train.py` script to download the dataset files from the Hugging Face CDN using the `dataset_files.json` file.
3. **Reuse existing Lightning Studio instances**: We will modify the script that creates the Lightning Studio instance to check if an instance with the same name already exists, and if so, reuse it instead of creating a new one.

#### Code Implementation

```python
import json
import os
import logging
from huggingface_hub import Repository

# Create a JSON file `dataset_files.json`
def create_dataset_files_json(repo_id):
    repo = Repository(local_dir='./', repo_id=repo_id)
    files = repo.list_repo_files(path='', recursive=False)
    with open('dataset_files.json', 'w') as f:
        json.dump(files, f)

# Modify the `train.py` script
def download_dataset_files():
    with open('dataset_files.json', 'r') as f:
        dataset_files = json.load(f)
    for file in dataset_files:
        file_path = f"https://huggingface.co/datasets/{file['repo']}/resolve/main/{file['path']}"
        logging.info(f"Downloading {file_path}")
        os.system(f"wget {file_path}")
        logging.info(f"Downloaded {file_path}")

# Reuse existing Lightning Studio instances
def create_lightning_studio_instance(instance_name):
    # Check if an instance with the same name already exists
    # If it does, reuse it instead of creating a new one
    # Implementation details omitted for brevity

# Example usage
repo_id = 'username/repo'
create_dataset_files_json(repo_id)
download_dataset_files()
```

#### Verification

To verify that the change works, we can run the `train.py` script and check that the dataset files are downloaded correctly from the Hugging Face CDN. We can also check the Hugging Face API logs to ensure that the API rate limit is not exceeded. Additionally, we can add logging statements to the `train.py` script to verify that the dataset files are downloaded correctly.

By implementing this solution, we can bypass the Hugging Face API rate limit for dataset training and reuse existing Lightning Studio instances efficiently, reducing wasted resources and quota.
