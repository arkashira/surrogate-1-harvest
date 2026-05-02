# Costinel / frontend

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement a fix for the HF API rate limit 429 (1000 req/5min) issue. This can be achieved by modifying the `list_repo_files` function to use `list_repo_tree(path, recursive=False)` per folder instead of recursively listing all files.

### Implementation Plan
1. **Identify the affected code**: Locate the code that uses `list_repo_files` and identify the areas where the HF API rate limit is being exceeded.
2. **Modify the code**: Replace `list_repo_files` with `list_repo_tree(path, recursive=False)` and iterate over the folders to list the files.
3. **Add pagination**: Implement pagination to handle large repositories and avoid exceeding the rate limit.
4. **Add retry mechanism**: Implement a retry mechanism to handle cases where the rate limit is exceeded and wait for 360s before retrying.

### Code Snippet
```python
import requests

def list_repo_files(repo_id, path):
    # Replace list_repo_files with list_repo_tree
    response = requests.get(f"https://huggingface.co/api/v1/repo/{repo_id}/tree/{path}", params={"recursive": False})
    files = response.json()["files"]
    # Iterate over folders and list files
    for folder in response.json()["folders"]:
        files.extend(list_repo_files(repo_id, f"{path}/{folder}"))
    return files

def get_files_with_pagination(repo_id, path, page_size=100):
    files = []
    page = 1
    while True:
        response = requests.get(f"https://huggingface.co/api/v1/repo/{repo_id}/tree/{path}", params={"recursive": False, "page": page, "page_size": page_size})
        files.extend(response.json()["files"])
        if not response.json()["has_next_page"]:
            break
        page += 1
    return files

def list_repo_files_with_retry(repo_id, path):
    try:
        return list_repo_files(repo_id, path)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            # Wait for 360s before retrying
            time.sleep(360)
            return list_repo_files_with_retry(repo_id, path)
        else:
            raise
```
### Deployment
The modified code can be deployed to the production environment after thorough testing to ensure that it resolves the HF API rate limit issue.
