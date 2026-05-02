# surrogate-1 / frontend

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits on the frontend side, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted resources and increased costs.
* The frontend does not have a mechanism to bypass the Hugging Face API rate limit, which can be achieved by using the HF CDN to download dataset files.
* The project does not have a clear strategy for handling Lightning Studio idle timeouts, which can kill training processes.
* The frontend may not be optimized for handling large datasets and may not be using the most efficient data loading strategies.

### Proposed change
The proposed change is to implement a robust mechanism for handling Hugging Face API rate limits and reusing existing Lightning Studio instances on the frontend side. This can be achieved by modifying the `train.py` file to use the HF CDN to download dataset files and implementing a studio reuse mechanism.

### Implementation
To implement the proposed change, the following steps can be taken:
1. Modify the `train.py` file to use the HF CDN to download dataset files. This can be done by replacing the `load_dataset` function with a custom function that downloads the dataset files from the HF CDN using the `requests` library.
2. Implement a studio reuse mechanism by modifying the `train.py` file to check if a Lightning Studio instance with the same name and status is already running. If it is, the script can reuse the existing instance instead of creating a new one.
3. Add a retry mechanism to handle Lightning Studio idle timeouts. This can be done by adding a try-except block around the `run` method and retrying the training process if it fails due to an idle timeout.

Example code snippet:
```python
import requests
import lightning

def download_dataset_from_cdn(dataset_name, dataset_path):
    url = f"https://huggingface.co/datasets/{dataset_name}/resolve/main/{dataset_path}"
    response = requests.get(url)
    with open(dataset_path, "wb") as f:
        f.write(response.content)

def reuse_studio(studio_name):
    for s in lightning.Teamspace.studios:
        if s.name == studio_name and s.status == "Running":
            return s
    return None

def train():
    dataset_name = "my_dataset"
    dataset_path = "my_dataset.parquet"
    studio_name = "my_studio"

    # Download dataset from HF CDN
    download_dataset_from_cdn(dataset_name, dataset_path)

    # Reuse existing studio instance
    studio = reuse_studio(studio_name)
    if studio is None:
        studio = lightning.Studio.create(name=studio_name)

    # Train model
    try:
        studio.run()
    except lightning.IdleTimeoutError:
        # Retry training process if idle timeout occurs
        studio.start(machine=lightning.Machine.L40S)
        studio.run()

if __name__ == "__main__":
    train()
```
### Verification
To verify that the proposed change works, the following steps can be taken:
1. Run the modified `train.py` file and verify that the dataset is downloaded from the HF CDN successfully.
2. Check the Lightning Studio dashboard to verify that the existing studio instance is reused instead of creating a new one.
3. Verify that the training process completes successfully and that the model is trained correctly.
4. Test the retry mechanism by simulating an idle timeout and verifying that the training process is retried successfully.
