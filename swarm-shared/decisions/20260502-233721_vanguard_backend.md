# vanguard / backend

**Most Valuable Next Improvement: Persistent File Manifest with Caching**

### Diagnosis

* No persisted file manifest → every run re-lists repos and re-checks schemas, leading to HF API 429 (1000 req/5min).
* Lightning Studio is recreated instead of reused, burning quota on recreation.
* Training process dies when studio stops, requiring restart with `target.start(machine=Machine.L40S)`.
* Repeated `list_repo_tree`/`load_dataset` calls lead to HF API 429 (1000 req/5min).

### Proposed Change

* Create a persistent file manifest that stores the list of files for each repo and date folder.
* Introduce a persistent file-list cache to store the list of files for each dataset.

### Implementation

1. Create a new file `manifest.py` in the root of the project:
```python
import json
import os

MANIFEST_FILE = 'manifest.json'

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_manifest(manifest):
    with open(MANIFEST_FILE, 'w') as f:
        json.dump(manifest, f)
```
2. Create a new file `dataset_cache.py` with the following content:
```python
import json
import os

CACHE_FILE = 'dataset_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

def get_file_list(dataset_name):
    cache = load_cache()
    if dataset_name in cache:
        return cache[dataset_name]
    # If not in cache, list files and save to cache
    file_list = list_repo_tree(dataset_name)
    save_cache({dataset_name: file_list})
    return file_list
```
3. Modify the `train.py` file to use the persistent manifest and cached file list:
```python
import manifest
import dataset_cache

def train():
    # Load manifest
    manifest = manifest.load_manifest()

    # Check if manifest is up-to-date
    if 'repo' in manifest and 'date' in manifest:
        # Use cached manifest
        repo = manifest['repo']
        date = manifest['date']
    else:
        # Re-list repo and date folder
        repo = 'my-repo'
        date = '2022-01-01'
        manifest['repo'] = repo
        manifest['date'] = date
        manifest.save_manifest()

    # Load cached file list
    file_list = dataset_cache.get_file_list(repo)

    # Use cached file list for training
    # ...
```
4. Modify the `lightning_studio.py` file to reuse the existing studio instance:
```python
import lightning

def create_studio():
    # Check if studio instance exists
    for s in lightning.Teamspace.studios:
        if s.name == 'my-studio' and s.status == 'Running':
            return s

    # Create new studio instance
    return lightning.Studio.create_ok(name='my-studio')
```
### Verification

1. Run the `train.py` script multiple times to verify that the file list is loaded from the cache instead of being listed again.
2. Check the cache file to ensure that it contains the expected data.
3. Verify that the HF API 429 error is no longer occurring due to repeated file listing.
4. Run the `lightning_studio.py` script to verify that the existing studio instance is reused instead of recreated.
