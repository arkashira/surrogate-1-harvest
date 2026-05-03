# airship / frontend

### Highest-Value Incremental Improvement (<2h)
#### Goal
Eliminate HF API 429s during Surrogate-1 training by replacing `load_dataset(streaming=True)` with a CDN-only `IterableDataset` that reads from a pre-generated `file_list.json`.

#### Why this ships fast
- Single-file change in `train.py`
- Leverages existing `list_repo_tree` API call
- Bypasses HF API rate limit using CDN

#### Implementation Plan
1. **Generate `file_list.json`**:
   - Run a single API call from Mac (after rate-limit window clears) to `list_repo_tree(path, recursive=False)` for one date folder.
   - Save the list to `file_list.json`.
2. **Create `IterableDataset`**:
   - Define a custom `IterableDataset` class that reads from `file_list.json`.
   - Implement `__iter__` to yield individual files from the list.
3. **Modify `train.py`**:
   - Replace `load_dataset(streaming=True)` with the custom `IterableDataset`.
   - Update the training loop to iterate over the `IterableDataset`.
4. **Test and Verify**:
   - Run a test training iteration to ensure the `IterableDataset` works correctly.
   - Verify that HF API 429s are eliminated during training.

#### Code Snippets
```python
import json
import os

# Generate file_list.json
def generate_file_list(repo, path):
    file_list = []
    for file in list_repo_tree(repo, path, recursive=False):
        file_list.append(file)
    with open('file_list.json', 'w') as f:
        json.dump(file_list, f)

# Create IterableDataset
class CDNIterableDataset(torch.utils.data.IterableDataset):
    def __init__(self, file_list):
        self.file_list = file_list

    def __iter__(self):
        for file in self.file_list:
            yield file

# Modify train.py
def train():
    # Load file_list.json
    with open('file_list.json', 'r') as f:
        file_list = json.load(f)

    # Create IterableDataset
    dataset = CDNIterableDataset(file_list)

    # Train model
    for file in dataset:
        # Process file
        pass
```
This implementation plan should take less than 2 hours to complete and will eliminate HF API 429s during Surrogate-1 training.
