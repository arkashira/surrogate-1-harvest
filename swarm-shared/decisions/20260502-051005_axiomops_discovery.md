# axiomops / discovery

### Highest-Value Incremental Improvement
The highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid rate-limit blocks during dataset training. This involves pre-listing file paths once, embedding them in the training script, and using the CDN to download dataset files without making API calls.

### Implementation Plan
1. **Identify the dataset repository**: Determine the repository containing the dataset to be used for training.
2. **Get the list of file paths**: Make a single API call to `list_repo_tree(path, recursive=False)` to get the list of file paths for the dataset.
3. **Save the list to a JSON file**: Save the list of file paths to a JSON file that can be embedded in the training script.
4. **Modify the training script**: Modify the training script to use the CDN to download dataset files instead of making API calls.
5. **Test the implementation**: Test the implementation to ensure that it works as expected and avoids rate-limit blocks.

### Code Snippets
```bash
# Get the list of file paths
file_paths=$(curl -X GET \
  https://huggingface.co/api/v1/datasets/{repo}/tree \
  -H 'Authorization: Bearer {token}' \
  -H 'Content-Type: application/json')

# Save the list to a JSON file
echo "$file_paths" > file_paths.json
```

```python
# Modify the training script to use the CDN
import json

with open('file_paths.json') as f:
    file_paths = json.load(f)

for file_path in file_paths:
    # Download the file from the CDN
    file_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    response = requests.get(file_url)
    with open(file_path, 'wb') as f:
        f.write(response.content)
```
This implementation should take less than 2 hours to complete and will improve the efficiency of dataset training by avoiding rate-limit blocks.
