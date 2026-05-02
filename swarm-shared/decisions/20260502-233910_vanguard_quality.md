# vanguard / quality

**Most Valuable Next Improvement: Persistent File-List Cache**

### Diagnosis

* No persistent file-list cache → repeated `list_repo_tree`/`load_dataset` → HF API 429 (1000 req/5min).
* Lightning Studio lifecycle not reused → quota burn on recreation.
* Training scripts re-fetch file lists on each run.

### Proposed Change

Implement a persistent file-list cache using a JSON file to store the list of files for each repository.

### Implementation

Create a new file `file_list_cache.py` in the root directory of the project:

```python
import json
import os

CACHE_DIR = '.cache'
CACHE_FILE_NAME = 'file_list_cache.json'

def load_file_list_cache(repo_name):
    cache_file = os.path.join(CACHE_DIR, f'{repo_name}_{CACHE_FILE_NAME}')
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            return json.load(f)
    return {}

def save_file_list_cache(repo_name, file_list):
    cache_file = os.path.join(CACHE_DIR, f'{repo_name}_{CACHE_FILE_NAME}')
    with open(cache_file, 'w') as f:
        json.dump(file_list, f)

def get_file_list(repo_name, path):
    cache = load_file_list_cache(repo_name)
    if repo_name in cache and path in cache[repo_name]:
        return cache[repo_name][path]
    # fetch file list from HF API and save to cache
    file_list = list_repo_tree(repo_name, path)
    save_file_list_cache(repo_name, {repo_name: {path: file_list}})
    return file_list
```

Update the `train.py` script to use the cached file list:

```python
import file_list_cache

# ...

file_list = file_list_cache.get_file_list(repo_name, path)
# ...
```

### Verification

1. Run the `train.py` file and verify that the file list is cached correctly.
2. Check the `.cache` directory to ensure that the cache file is created and updated correctly.
3. Monitor the HF API rate limit to ensure it's not being exceeded.

### Additional Improvements

1. **Cache expiration**: Implement a cache expiration mechanism to ensure that the cache is updated periodically.
2. **Cache size limit**: Implement a cache size limit to prevent the cache from growing indefinitely.
3. **Cache invalidation**: Implement a cache invalidation mechanism to ensure that the cache is updated when the underlying data changes.

### Example Use Cases

1. **Training scripts**: Use the cached file list to improve the performance of training scripts.
2. **Lightning Studio**: Use the cached studio lifecycle to improve the performance of Lightning Studio.
3. **Data loading**: Use the cached file list to improve the performance of data loading operations.
