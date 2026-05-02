# Costinel / frontend

**Task:** Implement HF CDN Bypass for Costinel's dataset training

**Pattern:** HF API rate-limit blocks dataset training

**Fix:** Public dataset files at `https://huggingface.co/datasets/{repo}/resolve/main/{path}` can be downloaded with NO Authorization header — bypasses /api/ auth-check rate limit entirely. CDN tier has separate (much higher) limits

**Implementation Plan:**

1. **Update `train.py`**: Modify the `train.py` script to use the CDN URL for dataset files instead of the API URL. This will bypass the rate limit check.
```python
import os
import requests

# ...

# Use CDN URL for dataset files
cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
response = requests.get(cdn_url)
if response.status_code == 200:
    # Process the dataset file
    ...
```
2. **Update `dataset-mirror` script**: Modify the `dataset-mirror` script to use the CDN URL for uploading dataset files.
```python
import os
import requests

# ...

# Use CDN URL for uploading dataset files
cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
response = requests.put(cdn_url, data=data)
if response.status_code == 200:
    # Process the uploaded dataset file
    ...
```
3. **Test the implementation**: Run the `train.py` script and verify that the dataset files are being downloaded from the CDN URL. Also, test the `dataset-mirror` script to ensure that dataset files are being uploaded to the CDN URL.

**Code Snippets:**

```python
# train.py
import os
import requests

# ...

# Use CDN URL for dataset files
cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
response = requests.get(cdn_url)
if response.status_code == 200:
    # Process the dataset file
    ...
```

```python
# dataset-mirror.py
import os
import requests

# ...

# Use CDN URL for uploading dataset files
cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
response = requests.put(cdn_url, data=data)
if response.status_code == 200:
    # Process the uploaded dataset file
    ...
```

**Estimated Time:** 30 minutes

**Tags:** #huggingface #cdn #rate-limit-bypass #training
