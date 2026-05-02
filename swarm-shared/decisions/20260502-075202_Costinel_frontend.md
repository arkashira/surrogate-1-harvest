# Costinel / frontend

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement a fix for the HF API rate limit 429 error. This error occurs when the number of requests exceeds 1000 per 5 minutes. 

### Implementation Plan
To fix this issue, we will:
1. **Avoid recursive `list_repo_files` calls**: Instead of using `list_repo_files` recursively, which can lead to a large number of requests, we will use `list_repo_tree` with `recursive=False` to fetch files in a specific folder.
2. **Paginate requests**: We will paginate our requests to ensure that we do not exceed the rate limit.
3. **Implement retry mechanism**: After encountering a 429 error, we will wait for 360 seconds before retrying the request.

### Code Snippet
```python
import requests
import time

def list_repo_files(repo_id, path):
    # Set API endpoint and headers
    url = f"https://huggingface.co/api/v1/repo/{repo_id}/tree/{path}"
    headers = {"Authorization": "Bearer YOUR_API_TOKEN"}

    # Initialize pagination parameters
    page = 0
    per_page = 100
    files = []

    while True:
        # Set pagination parameters
        params = {"page": page, "per_page": per_page}

        # Send request
        response = requests.get(url, headers=headers, params=params)

        # Check if rate limit is exceeded
        if response.status_code == 429:
            # Wait for 360 seconds before retrying
            time.sleep(360)
            continue

        # Parse response
        data = response.json()

        # Add files to list
        files.extend(data["files"])

        # Check if there are more pages
        if len(data["files"]) < per_page:
            break

        # Increment page
        page += 1

    return files
```
This implementation plan and code snippet should fix the HF API rate limit 429 error and improve the overall performance of the Costinel platform.
