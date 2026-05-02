# Costinel / backend

## Task: Implement HF CDN Bypass for Training Pipeline

### Highest-Value Incremental Improvement

* Fix: Implement HF CDN Bypass for Training Pipeline to avoid API rate limit 429 (1000 req/5min)
* Tags: #huggingface #cdn #rate-limit-bypass #training

### Implementation Plan

1. **Update `train.py` to use CDN downloads**
	* Replace `hf_hub_download` with CDN URL for dataset files
	* Example: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
2. **Implement rate limit handling**
	* Use `time.sleep(360)` to wait 360s before retrying after 429 error
	* Use `requests` library to handle API calls and rate limit handling
3. **Test and verify**
	* Run training pipeline with updated `train.py` to verify CDN bypass and rate limit handling

### Code Snippets

**train.py**
```python
import requests

# ...

def download_dataset(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.content
    elif response.status_code == 429:
        print("Rate limit exceeded, waiting 360s before retry")
        time.sleep(360)
        return download_dataset(repo, path)
    else:
        raise Exception(f"Failed to download dataset: {response.status_code}")

# ...
```
**requirements.txt**
```bash
requests
```
Note: This implementation plan assumes that the `train.py` script is the entry point for the training pipeline. The `download_dataset` function is used to download dataset files from the CDN. The `requests` library is used to handle API calls and rate limit handling. The `time.sleep` function is used to wait 360s before retrying after a 429 error.
