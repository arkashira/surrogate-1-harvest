# surrogate-1 / quality

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not have a mechanism to bypass the Hugging Face API rate limit for dataset training, resulting in potential training delays.
* The current implementation may not be optimized for dataset ingestion, potentially leading to inefficiencies in data processing.
* The project's training pipeline may not be resilient to failures, such as Lightning idle stop killing training processes.

### Proposed change
The proposed change will focus on implementing a mechanism to bypass the Hugging Face API rate limit for dataset training and reusing existing Lightning Studio instances. This will involve modifying the `train.py` script to use the Hugging Face CDN to download dataset files and implementing a check to reuse existing Lightning Studio instances before creating new ones.

### Implementation
To implement the proposed change, the following steps will be taken:
1. Modify the `train.py` script to use the Hugging Face CDN to download dataset files. This can be achieved by replacing the `load_dataset` function with a custom implementation that downloads the dataset files from the Hugging Face CDN using the `hf_hub_download` function.
2. Implement a check to reuse existing Lightning Studio instances before creating new ones. This can be achieved by adding a check to see if a Lightning Studio instance with the same name and status as the one being created already exists, and if so, reusing that instance instead of creating a new one.

Example code snippet:
```python
import os
import json
from lightning import Lightning
from huggingface_hub import hf_hub_download

# Define the Hugging Face CDN URL for the dataset
dataset_url = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

# Define the Lightning Studio instance name and status
studio_name = "surrogate-1-studio"
studio_status = "Running"

# Check if a Lightning Studio instance with the same name and status already exists
for s in Lightning.Teamspace.studios:
    if s.name == studio_name and s.status == studio_status:
        # Reuse the existing Lightning Studio instance
        studio = s
        break
else:
    # Create a new Lightning Studio instance
    studio = Lightning.Studio.create(name=studio_name)

# Download the dataset files from the Hugging Face CDN
dataset_files = hf_hub_download(dataset_url, repo="surrogate-1", path="data")

# Train the model using the downloaded dataset files
# ...
```
### Verification
To verify that the proposed change works, the following steps can be taken:
1. Run the modified `train.py` script and verify that it successfully downloads the dataset files from the Hugging Face CDN and trains the model without encountering any Hugging Face API rate limit errors.
2. Check the Lightning Studio instances and verify that the existing instance is being reused instead of creating a new one.
3. Monitor the training process and verify that it is resilient to failures, such as Lightning idle stop killing training processes.
