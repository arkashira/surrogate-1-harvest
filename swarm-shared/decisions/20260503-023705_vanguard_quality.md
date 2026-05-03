# vanguard / quality

### Diagnosis
* The project is currently using authenticated HF API calls for every preview/training launch, which burns quota and risks 429s.
* There is no static file manifest, resulting in every run re-enumerating the repo via API, leading to inefficiencies and potential rate limit issues.
* The project does not utilize the CDN bypass for training data fetches, which could significantly reduce the load on the HF API and mitigate rate limit issues.
* The lack of a systematic approach to handling rate limits and errors may lead to training interruptions and inefficiencies.
* Insufficient reuse of existing Lightning Studios may result in unnecessary quota usage and increased costs.

### Proposed Change
The proposed change involves implementing a CDN bypass for training data fetches, creating a static file manifest, and optimizing the reuse of existing Lightning Studios. This will be achieved by modifying the `train.py` script and the Lightning Studio launcher script.

### Implementation
1. **CDN Bypass**: Modify the `train.py` script to download training data from the HF CDN instead of using the HF API. This can be done by replacing the `load_dataset` function with a custom function that downloads the data from the CDN using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern.
2. **Static File Manifest**: Create a script that pre-lists the file paths for a given date folder using the `list_repo_tree` function and saves the list to a JSON file. This JSON file can then be embedded in the `train.py` script to avoid re-enumerating the repo via API on every run.
3. **Optimize Studio Reuse**: Modify the Lightning Studio launcher script to reuse existing Running studios instead of creating new ones. This can be done by listing the existing studios using the `Teamspace.studios` function and checking if a studio with the same name and status 'Running' already exists.

Example code snippets:
```python
import json
import requests

# CDN Bypass
def download_data_from_cdn(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    return response.content

# Static File Manifest
def pre_list_file_paths(repo, date_folder):
    file_paths = []
    for file in list_repo_tree(repo, path=date_folder, recursive=False):
        file_paths.append(file.path)
    with open("file_manifest.json", "w") as f:
        json.dump(file_paths, f)

# Optimize Studio Reuse
def reuse_existing_studio(name):
    for studio in Teamspace.studios:
        if studio.name == name and studio.status == 'Running':
            return studio
    return None
```
### Verification
To verify that the changes work as expected, the following steps can be taken:
1. Run the modified `train.py` script and verify that the training data is being downloaded from the HF CDN instead of the HF API.
2. Check the `file_manifest.json` file to ensure that it contains the correct list of file paths for the given date folder.
3. Verify that the Lightning Studio launcher script is reusing existing Running studios instead of creating new ones by checking the studio names and statuses in the Lightning dashboard.
4. Monitor the HF API usage and rate limit errors to ensure that the changes have reduced the load on the HF API and mitigated rate limit issues.
