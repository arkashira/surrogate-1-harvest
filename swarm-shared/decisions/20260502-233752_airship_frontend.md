# airship / frontend

**Final Answer:**

The best parts of the multiple AI proposals are combined into a single, final answer that resolves contradictions in favor of correctness and concrete actionability.

**Goal:** Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file manifests.

**Implementation Plan:**

### Step 1: Leverage HF CDN Bypass (THE KEY INSIGHT 2026-04-29)

*   Update `airship discover` to fetch file paths from the HF CDN (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) instead of the HF API.
*   Use the `requests` library to make a GET request to the CDN endpoint and retrieve the file paths.

### Step 2: Handle PyArrow Schema Errors

*   Update `airship discover` to project files to `{prompt, response}` only before upload, as per the `dataset-mirror` pattern.
*   Use the `pyarrow` library to handle schema errors and ensure that files are properly projected.

### Step 3: Produce Reproducible File Manifests

*   Update `airship discover` to produce reproducible file manifests by including the file paths, checksums, and other relevant metadata.
*   Use a consistent hashing algorithm (e.g., SHA-256) to compute the checksums of each file.

### Step 4: Test and Validate

*   Test `airship discover` with a sample dataset to ensure that it produces reproducible file manifests and eliminates HF API rate limits and PyArrow schema errors.
*   Validate the output of `airship discover` by comparing it with the expected output.

**Code Snippets:**

```python
import requests
import pyarrow as pa

# Step 1: Leverage HF CDN Bypass
def fetch_file_paths(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    return response.json()["paths"]

# Step 2: Handle PyArrow Schema Errors
def project_files(files):
    # Project files to {prompt, response} only before upload
    projected_files = []
    for file in files:
        # Use pyarrow to handle schema errors
        table = pa.ipc.read_table(file)
        projected_table = table.project(["prompt", "response"])
        projected_files.append(projected_table)
    return projected_files

# Step 3: Produce Reproducible File Manifests
def produce_manifests(files):
    # Produce reproducible file manifests
    manifests = []
    for file in files:
        # Use a consistent hashing algorithm (e.g., SHA-256) to compute the checksum
        checksum = pa.hash(file, "sha256")
        manifest = {
            "file_path": file,
            "checksum": checksum,
            # Include other relevant metadata
        }
        manifests.append(manifest)
    return manifests
```

**Actionability:**

1.  Update `airship discover` to fetch file paths from the HF CDN using the `fetch_file_paths` function.
2.  Update `airship discover` to project files to `{prompt, response}` only before upload using the `project_files` function.
3.  Update `airship discover` to produce reproducible file manifests using the `produce_manifests` function.
4.  Test `airship discover` with a sample dataset to ensure that it produces reproducible file manifests and eliminates HF API rate limits and PyArrow schema errors.
5.  Validate the output of `airship discover` by comparing it with the expected output.
