# airship / discovery

### Highest-Value Incremental Improvement for Airship Discovery
#### Improvement: Implement CDN-Only Ingestion for Surrogate-1 Training Pipeline

**Problem Statement**: HF API rate limits and commit caps hinder the Surrogate-1 training pipeline.

**Solution**:
1. **Pre-list file paths**: Make a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
2. **Save list to JSON**: Embed the list in the training script `train.py`.
3. **CDN-only fetches**: Use the CDN tier for dataset downloads, bypassing API rate limits.

**Implementation Plan**:
```markdown
### Step 1: Pre-list File Paths
* Make a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
* Save the list to a JSON file `file_paths.json`.

### Step 2: Modify Training Script
* Embed the `file_paths.json` list in the training script `train.py`.
* Use the CDN tier for dataset downloads.

### Step 3: Update Ingestion Pipeline
* Modify the ingestion pipeline to use the CDN-only approach.
* Ensure that the pipeline is deterministic and spreads writes across sibling repos.
```

**Code Snippets**:
```python
import json
import requests

# Pre-list file paths
def pre_list_file_paths(repo, path):
    response = requests.get(f"https://huggingface.co/api/v1/repo/{repo}/tree/{path}", params={"recursive": False})
    file_paths = response.json()["files"]
    with open("file_paths.json", "w") as f:
        json.dump(file_paths, f)

# Modify training script
def modify_training_script(file_paths):
    # Embed file paths in train.py
    with open("train.py", "r+") as f:
        content = f.read()
        f.seek(0)
        f.write(f"file_paths = {file_paths}\n" + content)

# Update ingestion pipeline
def update_ingestion_pipeline():
    # Modify ingestion pipeline to use CDN-only approach
    # Ensure deterministic writes across sibling repos
    pass
```

**Timeline**: This improvement can be implemented and shipped within 2 hours.
