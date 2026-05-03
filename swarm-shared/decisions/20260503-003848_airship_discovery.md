# airship / discovery

### Highest-Value Incremental Improvement
Implement a manifest-driven, CDN-only dataset loader to eliminate HF API rate limits and mixed-schema ingestion failures.

### Implementation Plan
1. **Create a manifest file**: Generate a JSON file containing the list of dataset files to be loaded. This can be done by running a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
2. **Embed the manifest in the training script**: Modify the training script to read the manifest file and use it to load the dataset files from the CDN.
3. **Use CDN-only dataset loading**: Update the dataset loading code to use the CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) instead of the HF API.
4. **Handle mixed-schema files**: Project the dataset files to `{prompt, response}` only at parse time to avoid mixed-schema issues.

### Code Snippets
```python
import json
import os

# Load the manifest file
with open('manifest.json', 'r') as f:
    manifest = json.load(f)

# Embed the manifest in the training script
def load_dataset(manifest):
    dataset = []
    for file in manifest:
        # Load the file from the CDN
        url = f"https://huggingface.co/datasets/{file['repo']}/resolve/main/{file['path']}"
        # ... (load the file and project to {prompt, response})
        dataset.append((prompt, response))
    return dataset

# Use CDN-only dataset loading
dataset = load_dataset(manifest)
```

### Example Use Case
```bash
# Generate the manifest file
python generate_manifest.py > manifest.json

# Run the training script with the manifest file
python train.py --manifest manifest.json
```

### Benefits
* Eliminates HF API rate limits
* Avoids mixed-schema ingestion failures
* Improves dataset loading performance by using the CDN

### Estimated Time to Ship
< 2 hours
