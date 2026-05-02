# Costinel / backend

## Task: Implement HF CDN Bypass for dataset training

### Problem Statement
The current implementation of dataset training is rate-limited by the Hugging Face API, which blocks training due to excessive API calls.

### Solution
Implement the HF CDN Bypass by downloading public dataset files directly from the Hugging Face CDN, which has a separate and much higher rate limit.

### Implementation Plan

#### Step 1: Pre-list file paths once and save to JSON

* Use `list_repo_tree(path, recursive=False)` to get a list of file paths for a specific date folder
* Save the list to a JSON file on the Mac (orchestration machine)
* Embed the JSON file path in the training script

```bash
#!/usr/bin/env bash

# Get list of file paths for a specific date folder
file_paths=$(list_repo_tree(path="/path/to/folder", recursive=False))

# Save list to JSON file
echo "$file_paths" > file_paths.json
```

#### Step 2: Modify training script to use CDN downloads

* Use the `file_paths.json` file to get the list of file paths
* Use `hf_hub_download` to download each file directly from the Hugging Face CDN
* Project to `{prompt, response}` only at parse time

```python
import json
import torch
from transformers import AutoTokenizer

# Load file paths from JSON file
with open('file_paths.json', 'r') as f:
    file_paths = json.load(f)

# Download files from Hugging Face CDN
for file_path in file_paths:
    file_url = f'https://huggingface.co/datasets/{repo}/resolve/main/{file_path}'
    file_path_local = hf_hub_download(file_url)
    # Project to {prompt, response} only at parse time
    tokenizer = AutoTokenizer.from_pretrained('model_name')
    inputs = tokenizer(file_path_local, return_tensors='pt')
    # ...
```

### Benefits

* Bypasses Hugging Face API rate limit
* Reduces API calls and training time
* Improves training efficiency and scalability

### Commit Message
`Implement HF CDN Bypass for dataset training`

### API Documentation
`list_repo_tree(path, recursive=False)`: Get list of file paths for a specific date folder
`hf_hub_download(file_url)`: Download a file directly from the Hugging Face CDN
