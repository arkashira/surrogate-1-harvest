# airship / discovery

### Highest-Value Incremental Improvement (<2h)
#### Goal
Eliminate HF API 429s during Surrogate-1 training by replacing `load_dataset(streaming=True)` with a CDN-only `IterableDataset` that reads from a pre-generated `file_list.json`.

#### Why this ships fast
- Single-file change in `train.py`
- Leverages existing `hf_hub_download` functionality
- Bypasses HF API rate limits using CDN

#### Implementation Plan
1. **Generate `file_list.json`**:
   - Run a script that uses `list_repo_tree(path, recursive=False)` to fetch file paths for a specific date folder.
   - Save the file paths to `file_list.json`.

2. **Create `IterableDataset`**:
   - Define a custom `IterableDataset` class that reads from `file_list.json`.
   - Use `hf_hub_download` to download files from the CDN.

3. **Replace `load_dataset`**:
   - In `train.py`, replace `load_dataset(streaming=True)` with an instance of the custom `IterableDataset` class.

#### Code Snippets
```python
# generate_file_list.py
import json
from huggingface_hub import list_repo_tree

def generate_file_list(repo_id, path):
    file_paths = list_repo_tree(repo_id, path, recursive=False)
    with open('file_list.json', 'w') as f:
        json.dump(file_paths, f)

# custom_dataset.py
import json
from torch.utils.data import IterableDataset
from huggingface_hub import hf_hub_download

class CustomDataset(IterableDataset):
    def __init__(self, file_list_path):
        self.file_list = json.load(open(file_list_path, 'r'))

    def __iter__(self):
        for file_path in self.file_list:
            # Download file from CDN using hf_hub_download
            file_content = hf_hub_download(file_path)
            # Yield file content
            yield file_content

# train.py
from custom_dataset import CustomDataset

def train():
    dataset = CustomDataset('file_list.json')
    # Train model using dataset
    pass
```
#### Example Use Case
- Run `generate_file_list.py` to generate `file_list.json` for a specific date folder.
- Run `train.py` to train the model using the custom `IterableDataset` class.

This implementation plan and code snippets provide a concrete solution to eliminate HF API 429s during Surrogate-1 training. By replacing `load_dataset(streaming=True)` with a CDN-only `IterableDataset`, we can bypass HF API rate limits and improve training efficiency.
