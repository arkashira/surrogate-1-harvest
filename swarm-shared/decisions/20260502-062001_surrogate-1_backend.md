# surrogate-1 / backend

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential downtime.
* The project does not have a clear strategy for handling file paths and list them only once to avoid repeated API calls.
* The current implementation does not check for studio status before each `.run()` call, which can lead to training process dying when studio stops.

**Proposed change**
* Implement HF CDN Bypass strategy by listing file paths once and embedding them in the training script.

**Implementation**
1. Create a new file `fetch_file_paths.py` in the project root with the following content:
```python
import os
import json
import requests

def fetch_file_paths(repo, path):
    # Clear rate limit window
    time.sleep(360)
    
    # List file paths using CDN
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    file_paths = response.json()
    
    # Save file paths to JSON
    with open(f"{repo}_{path}.json", "w") as f:
        json.dump(file_paths, f)

# Example usage
repo = "your-repo-name"
path = "your-date-folder"
fetch_file_paths(repo, path)
```
2. Modify the `train.py` script to load the file paths from the JSON file instead of making repeated API calls:
```python
import json

# Load file paths from JSON
with open(f"{repo}_{path}.json", "r") as f:
    file_paths = json.load(f)

# Use the file paths for training
```
3. Update the `train.py` script to check for studio status before each `.run()` call:
```python
import lightning as L

# Get the studio instance
studio = L.Studio.get()

# Check studio status
if studio.status != "Running":
    # Restart the studio instance
    studio.start(machine=L.Machine.L40S)
```
**Verification**
1. Run the `fetch_file_paths.py` script to generate the JSON file with file paths.
2. Verify that the `train.py` script loads the file paths correctly from the JSON file.
3. Check that the studio instance is reused correctly and the training process does not die when the studio stops.
4. Monitor the Hugging Face API rate limits and verify that the CDN Bypass strategy is effective in avoiding rate limit blocks.
