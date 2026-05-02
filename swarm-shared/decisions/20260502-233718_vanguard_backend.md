# vanguard / backend

**Most Valuable Next Improvement: Persistent File-List Cache with Reused Lightning Studio Lifecycle**

### Diagnosis

* No persistent file-list cache → repeated `list_repo_tree`/`load_dataset` → HF API 429 (1000 req/5min).
* Lightning Studio lifecycle not reused → quota burn on recreation.
* Training data loading is not optimized for CDN-only fetches.

### Proposed Change

Create a persistent file-list cache in `vanguard` backend to store the list of files for each dataset repository. Reuse the Lightning Studio lifecycle to avoid quota burn on recreation.

### Implementation

**Step 1: Implement persistent file-list cache**

* Create a new file `file_list_cache.py` in `vanguard/backend` directory:
```python
import json
import os

class FileListCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.cache_file = os.path.join(cache_dir, 'file_list_cache.json')

    def load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        return {}

    def save_cache(self, file_list):
        with open(self.cache_file, 'w') as f:
            json.dump(file_list, f)

    def get_file_list(self, repo, path):
        cache = self.load_cache()
        if repo in cache and path in cache[repo]:
            return cache[repo][path]
        return []

cache_dir = '/tmp/file_list_cache'
cache = FileListCache(cache_dir)
```
**Step 2: Modify training script to use cache**

* Modify `train.py` to use the cache:
```python
import file_list_cache

# ...

file_list = file_list_cache.get_file_list(repo, path)
if not file_list:
    file_list = load_dataset(repo, path)
    file_list_cache.save_cache(repo, path, file_list)

# ...
```
**Step 3: Reuse Lightning Studio lifecycle**

* Modify `lightning_studio.py` to reuse existing Studio instances:
```python
import lightning

# ...

studio = lightning.Studio()
if studio.exists():
    studio = lightning.Studio.load(studio.id)
else:
    studio = lightning.Studio.create()

# ...
```
**Step 4: Optimize training data loading for CDN-only fetches**

* Modify `load_dataset.py` to fetch data from CDN only:
```python
import requests

def load_dataset(repo, path):
    url = f'https://cdn.example.com/{repo}/{path}'
    response = requests.get(url)
    return response.json()
```
### Verification

1. Check that the cache directory is created and populated with file lists.
2. Verify that the `list_repo_tree`/`load_dataset` API calls are reduced.
3. Confirm that the Lightning Studio lifecycle is reused.
4. Check that the training data loading is optimized for CDN-only fetches.

**Additional Improvements**

* Consider implementing a cache expiration mechanism to ensure that the cache is updated periodically.
* Use a more robust caching mechanism, such as Redis or Memcached, to handle high traffic and concurrent requests.
* Implement a monitoring system to track cache hits and misses, and adjust the caching strategy accordingly.
* Consider using a more efficient data loading mechanism, such as parallel loading or chunking, to reduce the load on the CDN.
