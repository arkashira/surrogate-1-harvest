# surrogate-1 / backend

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not leverage the Hugging Face CDN to bypass API rate limits for dataset training.
* The existing implementation does not properly handle the Hugging Face API rate limit 429 (1000 req/5min) and commit cap 128/hr/repo blocks ingestion.

**Proposed change**
* Implement the HF CDN Bypass pattern to download public dataset files from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` without Authorization headers, bypassing the API rate limit.

**Implementation**
```bash
# Step 1: List repository tree for one date folder and save to JSON
python -c "import requests; import json; repo='your-repo'; date='your-date'; url=f'https://huggingface.co/datasets/{repo}/resolve/main/{date}'; response = requests.get(url); json.dump(response.json(), open('file_list.json', 'w'))"

# Step 2: Embed file list in train.py
# Add the following code to train.py
import json

with open('file_list.json') as f:
    file_list = json.load(f)

# Use the file list to download files from the Hugging Face CDN
for file in file_list:
    url = f'https://huggingface.co/datasets/{repo}/resolve/main/{file}'
    response = requests.get(url)
    # Process the file as needed
```

**Verification**
* Confirm that the HF CDN Bypass implementation is working by checking the API rate limit logs and ensuring that the dataset training process can complete without hitting the rate limit.
* Verify that the file list is being properly generated and used in the train.py script.
* Test the implementation with a sample dataset to ensure that it is working as expected.
