# surrogate-1 / backend

**Final Implementation Plan (≤2h)**

### Step 1: Pre-flight snapshot generation

Create a new script `bin/snapshot.sh` that generates a snapshot of the dataset repository for each shard:

```bash
#!/bin/bash

# Set repository and shard ID
REPO="axentx/surrogate-1-training-pairs"
SHARD_ID=$1

# Download the dataset repository
hf_hub_download "$REPO" --path "$SHARD_ID" --recursive --output-dir snapshots

# Get the list of files in the shard
files=$(ls snapshots/$SHARD_ID)

# Create a JSON file with the list of files
echo "{"
for file in $files; do
  echo "  \"$file\": {"
  echo "    \"path\": \"$file\""
  echo "  },"
done
echo "}"
> snapshots/$SHARD_ID.json
```

### Step 2: Update `bin/dataset-enrich.sh` to use pre-flight snapshot

Update the `bin/dataset-enrich.sh` script to use the pre-flight snapshot instead of loading the dataset at runtime:

```bash
#!/bin/bash

# Set repository and shard ID
REPO="axentx/surrogate-1-training-pairs"
SHARD_ID=$1

# Load pre-flight snapshot
snapshot=$(jq -r ".[] | select(.path == \"$SHARD_ID\") | .path" snapshots/$SHARD_ID.json)

# Stream and normalize the data
while IFS= read -r line; do
  # Process the line
  echo "$line"
done <<hf_hub_download "$REPO" --path "$snapshot" --recursive --output-dir data)
```

### Step 3: Update the GitHub Actions workflow to use the pre-flight snapshot

Update the GitHub Actions workflow to use the pre-flight snapshot instead of loading the dataset at runtime:

```yml
name: Ingest Surrogate-1 Training Pairs

on:
  workflow_dispatch:
    inputs:
      shard-id:
        description: 'Shard ID'
        required: true

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v2

      - name: Download pre-flight snapshot
        run: |
          ./bin/snapshot.sh ${{ inputs.shard-id }}

      - name: Ingest data
        run: |
          ./bin/dataset-enrich.sh ${{ inputs.shard-id }}
```

**Commit message:**

`feat: Replace runtime dataset loading with pre-flight snapshot`

**API documentation:**

`bin/snapshot.sh`: Generates a pre-flight snapshot of the dataset repository for a given shard ID.

`bin/dataset-enrich.sh`: Ingests data from a pre-flight snapshot of the dataset repository for a given shard ID.

GitHub Actions workflow: Ingests data from a pre-flight snapshot of the dataset repository for a given shard ID.

**Changes:**

* Replaced runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a deterministic pre-flight snapshot + CD.
* Updated `bin/snapshot.sh` to generate a snapshot of the dataset repository for each shard.
* Updated `bin/dataset-enrich.sh` to use the pre-flight snapshot instead of loading the dataset at runtime.
* Updated the GitHub Actions workflow to use the pre-flight snapshot instead of loading the dataset at runtime.

**Time complexity:**

* Generating the pre-flight snapshot: O(n), where n is the number of files in the dataset repository.
* Ingesting data from the pre-flight snapshot: O(n), where n is the number of files in the dataset repository.
* Total time complexity: O(n) + O(n) = O(2n), which simplifies to O(n).

**Space complexity:**

* Generating the pre-flight snapshot: O(n), where n is the number of files in the dataset repository.
* Ingesting data from the pre-flight snapshot: O(n), where n is the number of files in the dataset repository.
* Total space complexity: O(n) + O(n) = O(2n), which simplifies to O(n).
