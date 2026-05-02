# vanguard / backend

**Most Valuable Next Improvement: Persistent File-List Cache**

### Diagnosis

* No persistent file-list cache → repeated `list_repo_tree`/`load_dataset` → HF API 429 (1000 req/5min).
* Lightning Studio lifecycle not reused → quota burn on recreation.
* Training = re-fetching file-list on each run → inefficient.

### Proposed change

* Introduce a persistent file-list cache using a JSON file (`file_list_cache.json`) in the project root.

### Implementation

**Step 1: Create a cache file**

Create a new file `file_list_cache.json` in the project root with the following content:
```json
{
  "cache": {}
}
```
**Step 2: Modify `list_repo_tree` calls**

Modify the `list_repo_tree` calls to check if the file-list cache is up-to-date. If it is, use the cached result instead of making a new API call.

**Step 3: Update `load_dataset` to use the cache**

Update the `load_dataset` function to use the cached file-list instead of making a new API call.

**Step 4: Implement cache invalidation**

Implement a mechanism to invalidate the cache when the underlying data changes (e.g., when a new file is added or removed).

### Implementation code

```python
import json
import os

# Cache file path
CACHE_FILE = 'file_list_cache.json'

def get_file_list_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)['cache']
    return {}

def update_file_list_cache(file_list):
    with open(CACHE_FILE, 'w') as f:
        json.dump({'cache': file_list}, f)

def list_repo_tree(path, recursive=False):
    # Check if cache is up-to-date
    cache = get_file_list_cache()
    if path in cache:
        return cache[path]

    # Make API call to get file list
    file_list = api.list_repo_tree(path, recursive=recursive)

    # Update cache
    update_file_list_cache(file_list)

    return file_list

def load_dataset(streaming=False):
    # Use cached file-list
    file_list = get_file_list_cache()
    return hf.load_dataset(file_list)
```
### Verification

* Verify that the cache is being used correctly by checking the `list_repo_tree` calls.
* Verify that the cache is being updated correctly by checking the `update_file_list_cache` function.
* Verify that the cache is being invalidated correctly by checking the `get_file_list_cache` function.

### Additional improvements

* Implement a mechanism to reuse the Lightning Studio lifecycle when possible.
* Update the `train.py` script to use the file-list cache when loading datasets.
* Monitor the HF API usage and quota burn to ensure that the file-list cache is reducing the number of API calls and quota burns.

### Example use case

```python
# Load the file-list cache
file_list_cache = get_file_list_cache()

# Check if the file-list cache is up-to-date
if 'repo' in file_list_cache and 'date' in file_list_cache:
    # Use the cached file-list
    file_list = file_list_cache['repo']['date']
else:
    # Make an API call to retrieve the file-list
    file_list = api.list_repo_tree('repo', recursive=True)
    # Update the cache
    update_file_list_cache(file_list)
```
This code snippet demonstrates how to use the file-list cache to retrieve the file list for a given repository and date. If the cache is up-to-date, it uses the cached file list; otherwise, it makes an API call to retrieve the file list and updates the cache.
