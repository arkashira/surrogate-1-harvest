# airship / discovery

### Highest-Value Incremental Improvement
Implement HF CDN Bypass for Faster Data Ingestion

#### Problem Statement
The current data ingestion pipeline in Airship is limited by Hugging Face API rate limits, which slows down the training process.

#### Solution
Use the HF CDN Bypass pattern to download dataset files directly from the CDN, bypassing the API rate limit. This can be achieved by:

1. Pre-listing file paths once using the `list_repo_tree` API call
2. Embedding the file list in the training script
3. Using the CDN URL to download files directly, without making API calls during data load

#### Implementation Plan
1. **Update the data ingestion script** to use the HF CDN Bypass pattern:
	* Use `list_repo_tree` to pre-list file paths for a specific date folder
	* Save the file list to a JSON file
	* Update the training script to embed the file list and use CDN URLs for downloading files
2. **Modify the training script** to use the CDN URL for downloading files:
	* Use the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern to download files directly from the CDN
3. **Test the updated data ingestion pipeline** to ensure that it can bypass the API rate limit and download files faster

#### Code Snippets
```bash
# Pre-list file paths using list_repo_tree API call
file_list=$(curl -X GET \
  https://huggingface.co/api/v1/datasets/{repo}/tree/main/{path} \
  -H 'Authorization: Bearer {token}' \
  -H 'Content-Type: application/json')

# Save file list to a JSON file
echo "$file_list" > file_list.json

# Update training script to embed file list and use CDN URL
train.py:
import json

with open('file_list.json') as f:
  file_list = json.load(f)

for file in file_list:
  file_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file['path']}"
  # Download file using CDN URL
  curl -X GET "$file_url" -o "${file['path']}"
```
#### Estimated Time to Ship
< 2 hours

This incremental improvement can be shipped quickly, as it only requires updating the data ingestion script and training script to use the HF CDN Bypass pattern. The changes are relatively small and can be tested quickly to ensure that they work as expected.
