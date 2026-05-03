# airship / frontend

### Highest-Value Incremental Improvement (<2h)
#### Goal
Eliminate HF API 429s during Surrogate-1 training by replacing `load_dataset(streaming=True)` with a CDN-only `IterableDataset` that reads from a pre-generated `file_list.json`.

#### Why this ships fast
- Single-file change in `train.py`
- Leverages existing `list_repo_tree` API call
- Reduces API calls during training to zero

#### Implementation Plan
1. **Generate `file_list.json`**:
   - Run a script that calls `list_repo_tree(path, recursive=False)` for the desired dataset repository and date folder.
   - Save the result to `file_list.json` in the same directory as `train.py`.

2. **Modify `train.py`**:
   - Import necessary libraries, including `json` for loading `file_list.json`.
   - Define a custom `IterableDataset` class that reads files from the CDN using the pre-generated `file_list.json`.
   - Replace `load_dataset(streaming=True)` with an instance of the custom `IterableDataset` class.

#### Code Snippets
```python
import json
from torch.utils.data import IterableDataset
import torch

# Load file list from JSON
with open('file_list.json', 'r') as f:
    file_list = json.load(f)

class CDNIterableDataset(IterableDataset):
    def __init__(self, file_list):
        self.file_list = file_list

    def __iter__(self):
        for file_path in self.file_list:
            # Download file from CDN
            file_content = torch.load(f'https://huggingface.co/datasets/{file_path}')
            # Yield file content
            yield file_content

# Replace load_dataset with CDNIterableDataset
dataset = CDNIterableDataset(file_list)
```

#### Example Use Case
To use the new `CDNIterableDataset` in your training loop, simply iterate over the dataset as you would with any other PyTorch dataset:
```python
for batch in dataset:
    # Process batch
    pass
```
This change allows Surrogate-1 training to bypass HF API rate limits by downloading dataset files directly from the CDN, reducing the number of API calls to zero during training.
