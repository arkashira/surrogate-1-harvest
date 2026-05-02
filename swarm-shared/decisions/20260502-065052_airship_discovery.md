# airship / discovery

### Highest-Value Incremental Improvement
The highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate-limit blocks during dataset training. This involves pre-listing file paths once, embedding them in the training script, and using the CDN to download dataset files without authorization headers.

### Implementation Plan
1. **Identify the dataset repository**: Determine the repository containing the dataset to be used for training.
2. **List file paths**: Use the `list_repo_tree` API call to list the file paths in the repository for a specific date folder.
3. **Save file paths to JSON**: Save the listed file paths to a JSON file.
4. **Embed JSON in training script**: Modify the training script to read the file paths from the JSON file and use the CDN to download the dataset files.
5. **Update training script**: Update the training script to use the CDN to download dataset files without authorization headers.

### Code Snippets
```bash
# List file paths using list_repo_tree API call
repo_id="dataset/repo"
date_folder="2026-05-02"
file_paths=$(curl -X GET \
  https://huggingface.co/api/repo/list_repo_tree \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -d 'repo_id='"$repo_id"'&path='"$date_folder"'&recursive=false')

# Save file paths to JSON file
echo "$file_paths" > file_paths.json
```

```python
# Embed JSON in training script
import json

with open('file_paths.json') as f:
  file_paths = json.load(f)

# Use CDN to download dataset files
for file_path in file_paths:
  file_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
  # Download file using CDN
  response = requests.get(file_url)
  with open(file_path, 'wb') as f:
    f.write(response.content)
```
This implementation plan and code snippets provide a concrete solution to avoid API rate-limit blocks during dataset training by using the HF CDN Bypass pattern.
