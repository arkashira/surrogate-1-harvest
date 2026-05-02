# airship / discovery

### High-Value Incremental Improvement: Implementing HF CDN Bypass for Faster Data Ingestion
#### Problem Statement
The current data ingestion pipeline in Airship is limited by Hugging Face API rate limits, causing delays in training and model updates.

#### Solution Overview
To bypass the Hugging Face API rate limits, we can utilize the HF CDN to download dataset files directly. This approach eliminates the need for API calls during data loading, significantly reducing the ingestion time.

#### Implementation Plan
1. **Identify Dataset Files**: Use the Hugging Face API to retrieve a list of dataset files for a specific repository. This can be done using the `list_repo_tree` method with `recursive=False` to avoid excessive API calls.
2. **Download Dataset Files via CDN**: Use the retrieved file list to download the dataset files directly from the HF CDN. The file URLs can be constructed using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` pattern.
3. **Update Training Script**: Modify the training script to use the downloaded dataset files instead of relying on the Hugging Face API for data loading.

#### Code Snippets
```python
import requests
import json

# Retrieve dataset file list using Hugging Face API
def get_dataset_file_list(repo, path):
    api_url = f"https://huggingface.co/api/repos/{repo}/tree/{path}"
    response = requests.get(api_url)
    file_list = response.json()["files"]
    return file_list

# Download dataset files via HF CDN
def download_dataset_files(file_list, repo, path):
    for file in file_list:
        file_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}/{file}"
        response = requests.get(file_url)
        with open(file, "wb") as f:
            f.write(response.content)

# Update training script to use downloaded dataset files
def update_training_script(file_list, repo, path):
    # Load dataset files from local storage
    dataset_files = [open(file, "rb") for file in file_list]
    # Use dataset files for training
    # ...
```
#### Example Use Case
Suppose we want to train a model using the `my-dataset` repository. We can use the `get_dataset_file_list` function to retrieve the list of dataset files, and then download the files using the `download_dataset_files` function. Finally, we can update the training script to use the downloaded dataset files.
```python
repo = "my-dataset"
path = "data"
file_list = get_dataset_file_list(repo, path)
download_dataset_files(file_list, repo, path)
update_training_script(file_list, repo, path)
```
By implementing the HF CDN bypass, we can significantly reduce the data ingestion time and improve the overall performance of the Airship platform.
