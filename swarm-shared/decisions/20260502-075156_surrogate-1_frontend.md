# surrogate-1 / frontend

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits on the frontend side, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted resources and increased costs.
* The frontend may not be utilizing the HF CDN bypass effectively, resulting in unnecessary API calls and rate limit issues.
* The project may not be handling Lightning idle stop kills training effectively, leading to interrupted training processes.
* The frontend may not be optimized for performance, leading to slow training times and inefficient resource utilization.

### Proposed change
The proposed change will focus on implementing a robust HF CDN bypass and reusing existing Lightning Studio instances efficiently. This will involve modifying the `train.py` script to utilize the HF CDN bypass and implementing a studio reuse mechanism.

### Implementation
To implement the proposed change, the following steps will be taken:
1. Modify the `train.py` script to use the HF CDN bypass by downloading dataset files from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` instead of using the Hugging Face API.
2. Implement a studio reuse mechanism by listing existing Lightning Studio instances and reusing running ones before creating new ones.
3. Update the `train.py` script to handle Lightning idle stop kills training by checking the studio status before each `.run()` call and restarting the training process if the studio is stopped.

Example code snippet:
```python
import requests
import lightning

# HF CDN bypass
def download_dataset(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    return response.content

# Studio reuse mechanism
def reuse_studio(name):
    for s in lightning.Teamspace.studios:
        if s.name == name and s.status == 'Running':
            return s
    return None

# Handle Lightning idle stop kills training
def train(model, dataset):
    studio = reuse_studio('my-studio')
    if studio is None:
        studio = lightning.Studio(create_ok=True)
    try:
        studio.run(model, dataset)
    except lightning.IdleStopError:
        studio.start(machine=lightning.Machine.L40S)
        studio.run(model, dataset)
```
### Verification
To verify that the proposed change works, the following steps will be taken:
1. Run the modified `train.py` script and monitor the training process to ensure that it completes successfully without encountering rate limit issues or interrupted training processes.
2. Check the Lightning Studio instances to ensure that existing instances are being reused efficiently.
3. Monitor the training times and resource utilization to ensure that the frontend is optimized for performance.
4. Verify that the HF CDN bypass is working correctly by checking the dataset files downloaded from the CDN.
