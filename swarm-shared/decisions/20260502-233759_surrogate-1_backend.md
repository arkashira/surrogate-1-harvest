# surrogate-1 / backend

**Final Implementation Plan (≤2h)**

### Step 1: Pre-flight snapshot generation

Create a new script `bin/snapshot.sh` that generates a pre-flight snapshot of the dataset:

```bash
#!/bin/bash

# Set the HF token and dataset name
HF_TOKEN="your-hf-token"
DATASET_NAME="axentx/surrogate-1-training-pairs"

# Set the output directory for the snapshot
SNAPSHOT_DIR="batches/snapshot"

# Create the output directory
mkdir -p "$SNAPSHOT_DIR"

# Download the dataset files using the HF API
for file in $(huggingface_hub.list_repo_files "$DATASET_NAME" recursive=false); do
  hf_hub_download "$DATASET_NAME" "$file" -o "$SNAPSHOT_DIR/$file"
done

# Project the files to {prompt, response} only
for file in "$SNAPSHOT_DIR"/*.parquet; do
  pyarrow.parquet.read_table "$file" | 
  pyarrow.parquet.write_table("$file", columns=["prompt", "response"])
done

# Remove the original files
rm "$SNAPSHOT_DIR"/*.parquet
```

### Step 2: Update `bin/dataset-enrich.sh` to use the pre-flight snapshot

Update the `bin/dataset-enrich.sh` script to use the pre-flight snapshot instead of generating it on the fly:

```bash
#!/bin/bash

# Set the HF token and dataset name
HF_TOKEN="your-hf-token"
DATASET_NAME="axentx/surrogate-1-training-pairs"

# Set the output directory for the snapshot
SNAPSHOT_DIR="batches/snapshot"

# Use the pre-flight snapshot
for file in "$SNAPSHOT_DIR"/*.jsonl; do
  # Process the file
  # ...
done
```

### Step 3: Update the GitHub Actions workflow to use the pre-flight snapshot

Update the GitHub Actions workflow to use the pre-flight snapshot instead of generating it on the fly:

```yml
name: Ingest

on:
  workflow_dispatch:
    inputs:
      slug:
        description: 'Slug hash to ingest'
        required: true

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v2

      - name: Set up Docker
        uses: docker/setup-docker@v1

      - name: Run ingest script
        run: |
          bin/dataset-enrich.sh
```

**Implementation Time**: ≤2h

**Implementation Complexity**: Medium

**Testing**: Run the GitHub Actions workflow with a test slug hash to ensure the pre-flight snapshot is generated correctly and used in the ingest script.

**Key Insights**:

* The pre-flight snapshot generation script `bin/snapshot.sh` downloads the dataset files using the HF API and projects them to {prompt, response} only.
* The `bin/dataset-enrich.sh` script uses the pre-flight snapshot instead of generating it on the fly.
* The GitHub Actions workflow uses the pre-flight snapshot instead of generating it on the fly.

**Resolving Contradictions**:

* The three candidate implementation plans are combined into a single implementation plan that includes the pre-flight snapshot generation script, the updated `bin/dataset-enrich.sh` script, and the updated GitHub Actions workflow.
* The implementation plan is designed to be deterministic and efficient, using the pre-flight snapshot to reduce the runtime API calls and improve performance.

**Concrete Actionability**:

* The implementation plan provides a clear and concise set of steps to generate the pre-flight snapshot, update the `bin/dataset-enrich.sh` script, and update the GitHub Actions workflow.
* The plan includes a testing step to ensure the pre-flight snapshot is generated correctly and used in the ingest script.
