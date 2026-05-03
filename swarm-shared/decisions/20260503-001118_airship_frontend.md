# airship / frontend

### Highest-Value Incremental Improvement (<2h)
#### Goal
Implement a CDN-only `IterableDataset` for Surrogate-1 training to eliminate HF API 429s by reading from a pre-generated `file_list.json`.

#### Why this ships fast
- Single-file change in `train.py`
- Leverages existing `list_repo_tree` API call
- Reduces API calls during data loading to zero

#### Implementation Plan
1. **Generate `file_list.json`**:
   - Run a single API call from Mac (after rate-limit window clears) to `list_repo_tree(path, recursive=False)` for one date folder.
   - Save the list to `file_list.json`.

2. **Create `IterableDataset`**:
   - Define a custom `IterableDataset` class that reads from `file_list.json`.
   - Implement `__iter__` to yield individual files from the list.

3. **Modify `train.py`**:
   - Replace `load_dataset(streaming=True)` with the custom `IterableDataset`.
   - Ensure the dataset is loaded from the CDN using the pre-generated `file_list.json`.

#### Code Snippets
```python
import json
from torch.utils.data import IterableDataset
from torchvision import datasets
from torchvision.transforms import ToTensor

# Generate file_list.json
def generate_file_list(repo, path):
    import requests
    response = requests.get(f"https://huggingface.co/{repo}/tree/main/{path}")
    file_list = [file["path"] for file in response.json()["files"]]
    with open("file_list.json", "w") as f:
        json.dump(file_list, f)

# Custom IterableDataset
class CDNIterableDataset(IterableDataset):
    def __init__(self, file_list):
        self.file_list = file_list

    def __iter__(self):
        for file in self.file_list:
            yield file

# Modify train.py
def load_dataset():
    with open("file_list.json", "r") as f:
        file_list = json.load(f)
    dataset = CDNIterableDataset(file_list)
    return dataset
```
#### Example Use Case
```python
# Generate file_list.json
generate_file_list("username/repo", "path/to/files")

# Load dataset
dataset = load_dataset()

# Train model
for file in dataset:
    # Process file
    pass
```
