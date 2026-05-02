# surrogate-1 / quality

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted resources.
* The project does not have a clear strategy for handling dataset ingestion and training, which can lead to inefficiencies and errors.
* The codebase lacks a consistent approach to error handling and logging, making it difficult to diagnose and fix issues.
* The project's README is missing, which can make it difficult for new contributors to understand the project's goals and requirements.

### Proposed change
The proposed change is to implement a robust Hugging Face API rate limit handler and reuse existing Lightning Studio instances efficiently. This can be achieved by modifying the `train.py` file to use the HF CDN bypass strategy and implementing a studio reuse mechanism.

### Implementation
To implement the proposed change, follow these steps:
1. Modify the `train.py` file to use the HF CDN bypass strategy by downloading dataset files directly from the CDN instead of using the Hugging Face API.
2. Implement a studio reuse mechanism by listing existing Lightning Studio instances and reusing them if possible.
3. Update the `train.py` file to use the `list_repo_tree` method to get the list of dataset files and save it to a JSON file.
4. Modify the `train.py` file to read the list of dataset files from the JSON file and use it to download the files from the CDN.

Example code snippet:
```python
import json
import requests

# Get the list of dataset files from the JSON file
with open('dataset_files.json') as f:
    dataset_files = json.load(f)

# Download the dataset files from the CDN
for file in dataset_files:
    url = f'https://huggingface.co/datasets/{file}/resolve/main/{file}'
    response = requests.get(url)
    with open(file, 'wb') as f:
        f.write(response.content)
```
### Verification
To verify that the proposed change works, follow these steps:
1. Run the `train.py` file and check that it downloads the dataset files from the CDN instead of using the Hugging Face API.
2. Check that the studio reuse mechanism is working by listing the existing Lightning Studio instances and verifying that the script reuses them if possible.
3. Monitor the script's performance and check that it does not exceed the Hugging Face API rate limits.
4. Verify that the script logs any errors or issues that occur during execution and that it handles them correctly.
