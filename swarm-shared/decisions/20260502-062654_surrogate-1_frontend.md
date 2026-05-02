# surrogate-1 / frontend

### Diagnosis
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted quota and potential downtime.
* The frontend implementation does not leverage the HF CDN bypass, which can significantly reduce API calls and rate limit issues.
* The project does not have a clear strategy for handling file paths and listing them in a single API call.
* The frontend implementation may not be utilizing the existing design and quality cycles' decisions and findings.

### Proposed change
* Implement HF CDN bypass for dataset training by listing file paths in a single API call and embedding them in the training script.

### Implementation
1. In the `train.py` file, add a function to list file paths using the HF API:
```python
import requests

def list_file_paths(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    file_paths = response.json()["tree"]
    return file_paths
```
2. Modify the training script to use the `list_file_paths` function and save the file paths to a JSON file:
```python
file_paths = list_file_paths("your-repo", "your-date-folder")
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)
```
3. Embed the file paths in the training script by loading them from the JSON file:
```python
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)
```
4. Use the file paths to download the dataset files from the HF CDN:
```python
for file_path in file_paths:
    hf_hub_download(file_path)
```
5. Update the `requirements.txt` file to include the `requests` library.

### Verification
1. Run the training script and verify that it completes successfully without any rate limit issues.
2. Check the `file_paths.json` file to ensure that it contains the correct list of file paths.
3. Verify that the dataset files are downloaded correctly from the HF CDN.
4. Monitor the HF API rate limit logs to ensure that the rate limit is not exceeded.
