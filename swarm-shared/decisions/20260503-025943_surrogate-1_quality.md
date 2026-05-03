# surrogate-1 / quality

### Surrogate-1 Quality Improvement
#### Implementation Plan (≤2h)

To improve the quality of the surrogate-1 project, we will replace the existing `bin/dataset-enrich.sh` script with a new Python script, `bin/dataset-enrich.py`. This new script will utilize a manifest-driven approach and leverage the CDN-bypass ingestion method to enhance performance and efficiency.

#### Requirements

* `SHARD_ID`
* `SHARD_TOTAL=16`
* `DATE`
* `HF_TOKEN`

#### Implementation Steps

1. **Create `bin/dataset-enrich.py`**:
   ```python
import os
import json
import requests
from datasets import load_dataset
from huggingface_hub import Repository

def dataset_enrich(shard_id, shard_total, date, hf_token):
    # Initialize variables
    repo_id = "axentx/surrogate-1-training-pairs"
    repo = Repository(local_dir="/tmp/" + repo_id, repo_id=repo_id, token=hf_token)
    
    # List files in the repository for the given date
    files = repo.list_repo_tree(path=date, recursive=False)
    
    # Filter files for the current shard
    shard_files = [file for file in files if int(file.split("-")[0]) % shard_total == shard_id]
    
    # Download files from the CDN
    for file in shard_files:
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{date}/{file}"
        response = requests.get(url)
        if response.status_code == 200:
            # Process and upload the file
            with open(f"/tmp/{file}", "wb") as f:
                f.write(response.content)
            # Upload the file to the repository
            repo.upload_file(f"/tmp/{file}", f"batches/public-merged/{date}/{shard_id}-{file}")
        else:
            print(f"Failed to download {file}")

if __name__ == "__main__":
    import sys
    shard_id = int(sys.argv[1])
    shard_total = int(sys.argv[2])
    date = sys.argv[3]
    hf_token = sys.argv[4]
    dataset_enrich(shard_id, shard_total, date, hf_token)
```

2. **Modify the workflow to use the new script**:
   Update the `.github/workflows/ingest.yml` file to use the new `bin/dataset-enrich.py` script:
   ```yml
name: Ingest

on:
  workflow_dispatch:
  schedule:
    - cron:  '*/30 * * * *'

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard-id: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    steps:
      - name: Checkout code
        uses: actions/checkout@v3
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.x'
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install datasets huggingface_hub requests
      - name: Run ingestion script
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          DATE: ${{ github.event.schedule }}
        run: |
          python bin/dataset-enrich.py ${{ matrix.shard-id }} 16 $DATE $HF_TOKEN
```

3. **Commit and push the changes**:
   Commit the changes to the `bin/dataset-enrich.py` script and the `.github/workflows/ingest.yml` file, then push them to the repository.

#### Benefits

* Improved performance by utilizing the CDN-bypass ingestion method
* Enhanced efficiency by processing files in parallel across multiple shards
* Simplified workflow management using a manifest-driven approach

#### Next Steps

* Monitor the workflow runs and verify that the new script is working correctly
* Investigate any issues that arise and make adjustments as needed
* Consider implementing additional features, such as error handling and logging, to further improve the reliability and maintainability of the workflow.
