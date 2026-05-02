# surrogate-1 / frontend

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits on the frontend, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted resources.
* The frontend does not have a clear strategy for downloading dataset files, potentially leading to rate limit issues.
* The project does not have a mechanism to check the status of Lightning Studio instances before running training scripts.
* The frontend may not be properly handling errors and exceptions, potentially leading to crashes or unexpected behavior.

### Proposed change
The proposed change is to implement a robust Hugging Face API rate limit handling mechanism on the frontend, reuse existing Lightning Studio instances, and download dataset files using the HF CDN bypass strategy. The scope of this change includes modifying the `train.py` script and potentially adding new scripts or functions to handle rate limit checking and studio instance reuse.

### Implementation
To implement this change, the following steps can be taken:
1. Modify the `train.py` script to use the HF CDN bypass strategy for downloading dataset files. This can be done by replacing the `load_dataset` function with a custom function that downloads files from the HF CDN using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern.
2. Add a function to check the status of Lightning Studio instances before running training scripts. This can be done by using the Lightning API to query the status of studio instances and restarting them if they are stopped.
3. Implement a rate limit checking mechanism to prevent the frontend from exceeding the Hugging Face API rate limits. This can be done by tracking the number of API requests made and waiting for a certain amount of time before making additional requests.
4. Modify the `train.py` script to reuse existing Lightning Studio instances instead of creating new ones. This can be done by querying the Lightning API for existing studio instances and reusing them if they are available.

Example code snippet:
```python
import requests
import time

def download_dataset_files(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.content
    else:
        raise Exception(f"Failed to download dataset file: {url}")

def check_studio_status(studio_name):
    # Query the Lightning API to get the status of the studio instance
    studio_status = lightning_api.get_studio_status(studio_name)
    if studio_status == "stopped":
        # Restart the studio instance if it is stopped
        lightning_api.restart_studio(studio_name)
    return studio_status

def train_model(dataset_files, studio_name):
    # Check the status of the studio instance before running the training script
    studio_status = check_studio_status(studio_name)
    if studio_status == "running":
        # Download the dataset files using the HF CDN bypass strategy
        dataset_content = download_dataset_files("axentx", "dataset.parquet")
        # Run the training script
        # ...
    else:
        raise Exception(f"Studio instance {studio_name} is not running")

# Implement a rate limit checking mechanism
rate_limit_counter = 0
rate_limit_wait_time = 360  # seconds

def make_api_request():
    global rate_limit_counter
    global rate_limit_wait_time
    if rate_limit_counter >= 1000:
        # Wait for the rate limit wait time before making additional requests
        time.sleep(rate_limit_wait_time)
        rate_limit_counter = 0
    # Make the API request
    # ...
    rate_limit_counter += 1
```
### Verification
To verify that the changes work as expected, the following steps can be taken:
1. Run the modified `train.py` script and verify that it downloads dataset files using the HF CDN bypass strategy.
2. Check the status of Lightning Studio instances before running the training script and verify that it reuses existing instances or restarts them if they are stopped.
3. Verify that the rate limit checking mechanism prevents the frontend from exceeding the Hugging Face API rate limits.
4. Test the modified `train.py` script with different scenarios, such as running multiple training scripts concurrently or running training scripts with different dataset files.
