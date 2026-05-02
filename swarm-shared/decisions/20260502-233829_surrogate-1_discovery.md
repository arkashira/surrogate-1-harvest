# surrogate-1 / discovery

**Implementation Plan (≤2h)**

### Step 1: Pre-flight snapshot generation

Create a new script `bin/snapshot.sh` that generates a snapshot of the dataset repository by:

```bash
#!/bin/bash

# Set the dataset repository and snapshot directory
REPO="axentx/surrogate-1-training-pairs"
SNAPSHOT_DIR="batches/snapshot"

# Use `list_repo_tree` to get the list of files in the repository
files=$(huggingface_hub.api.env.hf_api_env "list_repo_tree($REPO, recursive=False)")

# Save the list of files to a JSON file
echo "$files" > "$SNAPSHOT_DIR/files.json"

# Use `hf_hub_download` to download each file individually
for file in $(jq -r '.[] | .path' "$SNAPSHOT_DIR/files.json"); do
  hf_hub_download "$REPO" "$file" --path "$SNAPSHOT_DIR/$file"
done
```

### Step 2: Update `bin/dataset-enrich.sh` to use the pre-flight snapshot

Update the `bin/dataset-enrich.sh` script to use the pre-flight snapshot instead of loading the dataset at runtime:

```bash
#!/bin/bash

# Set the snapshot directory and shard ID
SNAPSHOT_DIR="batches/snapshot"
SHARD_ID="$1"

# Load the snapshot of the dataset repository
files=$(jq -r ".[] | select(.shard_id == \"$SHARD_ID\") | .path" "$SNAPSHOT_DIR/files.json")

# Process each file in the snapshot
for file in $files; do
  # Use `hf_hub_download` to download the file from the snapshot directory
  hf_hub_download "$SNAPSHOT_DIR" "$file" --path "$file"

  # Process the file as before
  # ...
done
```

### Step 3: Update the GitHub Actions workflow to use the pre-flight snapshot

Update the GitHub Actions workflow to use the pre-flight snapshot instead of loading the dataset at runtime:

```yml
name: Ingest

on:
  workflow_dispatch:
    inputs:
      shard_id:
        description: Shard ID
        required: true

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v2

      - name: Run dataset-enrich.sh
        run: |
          bin/dataset-enrich.sh ${{ inputs.shard_id }}
```

### Step 4: Update `bin/snapshot.sh` to include robust error handling

Update `bin/snapshot.sh` to include robust error handling to ensure that the script can recover from unexpected errors:

```bash
#!/bin/bash

# Set the dataset repository and snapshot directory
REPO="axentx/surrogate-1-training-pairs"
SNAPSHOT_DIR="batches/snapshot"

# Use `list_repo_tree` to get the list of files in the repository
files=$(huggingface_hub.api.env.hf_api_env "list_repo_tree($REPO, recursive=False)")

# Save the list of files to a JSON file
echo "$files" > "$SNAPSHOT_DIR/files.json"

# Use `hf_hub_download` to download each file individually
for file in $(jq -r '.[] | .path' "$SNAPSHOT_DIR/files.json"); do
  if ! hf_hub_download "$REPO" "$file" --path "$SNAPSHOT_DIR/$file"; then
    echo "Error downloading file $file"
    exit 1
  fi
done
```

### Step 5: Update `bin/dataset-enrich.sh` to include robust error handling

Update `bin/dataset-enrich.sh` to include robust error handling to ensure that the script can recover from unexpected errors:

```bash
#!/bin/bash

# Set the snapshot directory and shard ID
SNAPSHOT_DIR="batches/snapshot"
SHARD_ID="$1"

# Load the snapshot of the dataset repository
files=$(jq -r ".[] | select(.shard_id == \"$SHARD_ID\") | .path" "$SNAPSHOT_DIR/files.json")

# Process each file in the snapshot
for file in $files; do
  # Use `hf_hub_download` to download the file from the snapshot directory
  if ! hf_hub_download "$SNAPSHOT_DIR" "$file" --path "$file"; then
    echo "Error downloading file $file"
    exit 1
  fi

  # Process the file as before
  # ...
done
```

### Step 6: Update the GitHub Actions workflow to include robust error handling

Update the GitHub Actions workflow to include robust error handling to ensure that the workflow can recover from unexpected errors:

```yml
name: Ingest

on:
  workflow_dispatch:
    inputs:
      shard_id:
        description: Shard ID
        required: true

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v2

      - name: Run dataset-enrich.sh
        run: |
          bin/dataset-enrich.sh ${{ inputs.shard_id }}
        retry: 3
        retry-on: failure
```

This implementation plan should take ≤2h to complete and will include robust error handling to ensure that the script can recover from unexpected errors.
