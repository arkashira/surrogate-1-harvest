# surrogate-1 / backend

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits on the backend side, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted resources and increased costs.
* The project does not have a mechanism to bypass the Hugging Face API rate limit for dataset training, which can cause significant delays.
* The current implementation does not handle the Lightning H200 instance not being available in the default cloud account, which can lead to training failures.
* The project does not have a robust way to handle the HF commit cap, which can block ingestion and training.

### Proposed change
The proposed change is to implement a robust mechanism to handle Hugging Face API rate limits, reuse existing Lightning Studio instances, and bypass the API rate limit for dataset training. This can be achieved by modifying the `train.py` script to use the HF CDN bypass and implementing a studio reuse mechanism.

### Implementation
To implement the proposed change, the following steps can be taken:
1. Modify the `train.py` script to use the HF CDN bypass by downloading dataset files from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` instead of using the Hugging Face API.
2. Implement a studio reuse mechanism by listing existing Lightning Studio instances and reusing the ones that are already running.
3. Handle the Lightning H200 instance not being available in the default cloud account by sweeping through different cloud accounts and instance sizes.
4. Implement a mechanism to handle the HF commit cap by spreading writes across multiple sibling repositories.

Example code snippet:
```python
import os
import requests

# HF CDN bypass
def download_dataset(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    with open(path, "wb") as f:
        f.write(response.content)

# Studio reuse mechanism
def reuse_studio(studio_name):
    for s in Teamspace.studios:
        if s.name == studio_name and s.status == "Running":
            return s
    return None

# Handle Lightning H200 instance not being available
def get_available_instance():
    for cloud_account in ["lightning-lambda-prod", "lightning-public-prod"]:
        for instance_size in ["L40S", "H200"]:
            try:
                instance = Machine(instance_size, cloud_account=cloud_account)
                return instance
            except Exception as e:
                print(f"Error getting instance {instance_size} in {cloud_account}: {e}")
    return None

# Handle HF commit cap
def spread_writes_across_repos(repo, data):
    repos = [f"{repo}-{i}" for i in range(5)]
    for repo in repos:
        try:
            # Write data to repo
            pass
        except Exception as e:
            print(f"Error writing to repo {repo}: {e}")
```

### Verification
To verify that the proposed change works, the following steps can be taken:
1. Run the modified `train.py` script and verify that it can download dataset files from the HF CDN bypass.
2. Check the Lightning Studio instances and verify that the reuse mechanism is working correctly.
3. Verify that the instance sweep mechanism is working correctly by checking the available instances in different cloud accounts.
4. Verify that the HF commit cap mechanism is working correctly by checking the writes to multiple sibling repositories.
5. Monitor the project's performance and verify that the proposed change has improved the handling of Hugging Face API rate limits, reused existing Lightning Studio instances, and bypassed the API rate limit for dataset training.
