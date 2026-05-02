# surrogate-1 / discovery

### Diagnosis
* The project lacks a robust implementation for data ingestion, relying heavily on the Hugging Face API with rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential training interruptions.
* The project does not utilize the HF CDN bypass strategy to download dataset files, which can help avoid API rate limits.
* The training pipeline is not optimized for performance, with potential issues such as pyarrow CastError on HF datasets with mixed schema files.
* The project does not have a mechanism to handle Lightning idle stop, which can kill training processes.

### Proposed change
The proposed change is to implement the HF CDN bypass strategy to download dataset files, which can help avoid API rate limits and improve the overall performance of the training pipeline. This change will be implemented in the `train.py` file, which is responsible for downloading and processing the dataset files.

### Implementation
To implement the HF CDN bypass strategy, we need to make the following changes:
```python
import json
import requests

# Download the list of file paths from the HF API
def download_file_paths(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    file_paths = response.json()
    return file_paths

# Save the list of file paths to a JSON file
def save_file_paths(file_paths, file_name):
    with open(file_name, "w") as f:
        json.dump(file_paths, f)

# Load the list of file paths from the JSON file
def load_file_paths(file_name):
    with open(file_name, "r") as f:
        file_paths = json.load(f)
    return file_paths

# Download the dataset files using the HF CDN bypass strategy
def download_dataset_files(file_paths):
    dataset_files = []
    for file_path in file_paths:
        url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
        response = requests.get(url)
        dataset_files.append(response.content)
    return dataset_files

# Update the train.py file to use the HF CDN bypass strategy
def train():
    # Download the list of file paths
    file_paths = download_file_paths(repo, path)
    save_file_paths(file_paths, "file_paths.json")

    # Load the list of file paths
    file_paths = load_file_paths("file_paths.json")

    # Download the dataset files
    dataset_files = download_dataset_files(file_paths)

    # Process the dataset files
    # ...
```
We also need to update the `train.py` file to reuse existing Lightning Studio instances, which can help avoid wasted quota and potential training interruptions.
```python
import lightning

# Reuse existing Lightning Studio instances
def reuse_studio():
    studios = lightning.Teamspace.studios
    for studio in studios:
        if studio.name == "surrogate-1" and studio.status == "Running":
            return studio
    return None

# Update the train.py file to reuse existing Lightning Studio instances
def train():
    # Reuse existing Lightning Studio instances
    studio = reuse_studio()
    if studio is None:
        # Create a new Lightning Studio instance
        studio = lightning.Studio(create_ok=True)

    # ...
```
### Verification
To verify that the changes work, we can run the `train.py` file and check that the dataset files are downloaded successfully using the HF CDN bypass strategy. We can also check that the existing Lightning Studio instances are reused correctly.
```bash
python train.py
```
We can also check the logs to see that the dataset files are downloaded successfully and that the training pipeline is running without any issues.
```bash
tail -f logs/train.log
```
We can also use the Lightning Studio dashboard to check that the studio instances are reused correctly and that the training pipeline is running without any issues.
