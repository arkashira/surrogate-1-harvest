# surrogate-1 / discovery

### Highest-Value Incremental Improvement
Implement a pre-flight file-list using the HF CDN bypass to reduce API calls and improve ingestion efficiency.

### Implementation Plan
1. **Modify `dataset-enrich.sh`**: Add a step to download the file list for the current date partition using the HF CDN bypass.
2. **Use `list_repo_tree` with `recursive=False`**: Call the HF API to list files in the current date partition, and save the response to a JSON file.
3. **Embed file list in `dataset-enrich.sh`**: Read the saved JSON file and use the file list to stream files from the HF CDN, bypassing the API rate limit.
4. **Update `ingest.yml` workflow**: Add a step to run the modified `dataset-enrich.sh` script with the pre-flight file-list.

### Code Snippets
```bash
# dataset-enrich.sh
#!/usr/bin/env bash

# Download file list for current date partition using HF CDN bypass
DATE_PARTITION=$(date +"%Y/%m/%d")
FILE_LIST_URL="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${DATE_PARTITION}"
FILE_LIST_JSON="file_list_${DATE_PARTITION}.json"

curl -s -o ${FILE_LIST_JSON} ${FILE_LIST_URL}

# Embed file list in dataset-enrich.sh
FILE_LIST=$(jq -r '.[] | .filename' ${FILE_LIST_JSON})

for FILE in ${FILE_LIST}; do
  # Stream file from HF CDN
  FILE_URL="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${DATE_PARTITION}/${FILE}"
  curl -s -o ${FILE} ${FILE_URL}
  # Process file...
done
```

```yml
# ingest.yml
name: Ingest

on:
  schedule:
    - cron: 0 0/30 * * * *

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v3
      - name: Run dataset-enrich.sh
        run: |
          bash dataset-enrich.sh
      - name: Upload output
        uses: actions/upload-artifact@v3
        with:
          name: output
          path: batches/public-merged/${DATE_PARTITION}
```
This implementation plan and code snippet demonstrate how to implement a pre-flight file-list using the HF CDN bypass to reduce API calls and improve ingestion efficiency. The modified `dataset-enrich.sh` script downloads the file list for the current date partition using the HF CDN bypass, embeds the file list in the script, and streams files from the HF CDN. The updated `ingest.yml` workflow runs the modified `dataset-enrich.sh` script and uploads the output.
