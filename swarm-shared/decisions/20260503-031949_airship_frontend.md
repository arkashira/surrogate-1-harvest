# airship / frontend

### Implementation Plan (≤2h)
#### Highest-value incremental improvement: 
Add a zero-API-training mode + Lightning auto-recovery to make Surrogate training rate-limit-proof and survive idle timeouts.

### Steps (concrete, 90min total)
1. **Pre-list CDN file manifest (15m)**
   - Run a single API call from Mac to `list_repo_tree(path, recursive=False)` for one date folder.
   - Save the list to a JSON file (`cdn_file_manifest.json`).
   - Embed this JSON file in the training script (`train.py`).

2. **Modify train.py for CDN-only fetches (20m)**
   - Update `train.py` to use the embedded `cdn_file_manifest.json` for data loading.
   - Use CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for downloading dataset files without API calls.
   - Ensure the training script can handle the new file manifest format.

3. **Implement Lightning auto-recovery (30m)**
   - Check the status of the Lightning studio before each `.run()` call.
   - If the studio is stopped, restart it with `target.start(machine=Machine.L40S)` before proceeding.
   - Add error handling to catch and recover from idle timeouts.

4. **Test and verify (25m)**
   - Run the modified training script with the new CDN-only fetches and auto-recovery.
   - Verify that the training process survives idle timeouts and does not hit rate limits.

### Code Snippets
#### Pre-listing CDN file manifest
```bash
# list_repo_tree.py
import json
import requests

def list_repo_tree(repo, path):
    url = f"https://huggingface.co/api/repos/{repo}/tree/main/{path}"
    response = requests.get(url)
    return response.json()

# Save the list to a JSON file
repo = "your-repo"
path = "your-path"
file_manifest = list_repo_tree(repo, path)
with open("cdn_file_manifest.json", "w") as f:
    json.dump(file_manifest, f)
```

#### Modifying train.py for CDN-only fetches
```python
# train.py
import json

# Load the embedded file manifest
with open("cdn_file_manifest.json", "r") as f:
    file_manifest = json.load(f)

# Use CDN URLs for downloading dataset files
def load_dataset(file_manifest):
    dataset = []
    for file in file_manifest:
        url = f"https://huggingface.co/datasets/{file['repo']}/resolve/main/{file['path']}"
        # Download the file using the CDN URL
        response = requests.get(url)
        # Process the downloaded file
        dataset.append(process_file(response.content))
    return dataset

# Train the model using the loaded dataset
dataset = load_dataset(file_manifest)
model = train_model(dataset)
```

#### Implementing Lightning auto-recovery
```python
# train.py
import lightning

# Check the status of the Lightning studio
def check_studio_status(studio):
    if studio.status == "Stopped":
        # Restart the studio
        studio.start(machine=lightning.Machine.L40S)

# Run the training process with auto-recovery
def run_training(studio):
    try:
        # Check the studio status before running
        check_studio_status(studio)
        # Run the training process
        studio.run()
    except lightning.IdleTimeoutError:
        # Recover from idle timeout
        studio.start(machine=lightning.Machine.L40S)
        # Retry the training process
        run_training(studio)
```
