# surrogate-1 / quality

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and resolve any contradictions in favor of correctness and concrete actionability.

**Key Insights:**

1. **Pre-flight snapshot generator**: Both proposals suggest creating a `bin/snapshot.sh` script that lists dataset files once per date folder and emits a deterministic file manifest. This approach eliminates the risk of API rate limits during data loading and ensures reliable ingestion.
2. **Deterministic file manifest**: The manifest should contain a JSON array of objects with `path`, `size`, and `sha` properties, sorted deterministically by path to ensure stable shards across runs.
3. **CDN-only fetches**: Training scripts should use the manifest to fetch files via the HF CDN (`resolve/main/...`) with zero API calls during data loading, removing the risk of 429 errors.
4. **Lightweight retry/backoff**: Implementing a lightweight retry mechanism with a backoff strategy (e.g., waiting 360s before retrying once) can help handle occasional API errors.

**Synthesized Solution:**

Create a `bin/snapshot.sh` script that:

1. Accepts `REPO` (default `axentx/surrogate-1-training-pairs`) and optional `DATE` (YYYY-MM-DD) as inputs.
2. Uses the `huggingface_hub` Python helper to `list_repo_tree` for the date folder (non-recursive) and emits a deterministic file manifest.
3. The manifest should contain a JSON array of objects with `path`, `size`, and `sha` properties, sorted deterministically by path.
4. Implement a lightweight retry mechanism with a backoff strategy to handle occasional API errors.

**Example Code:**

The provided code snippets can be combined and refined to create a robust `bin/snapshot.sh` script. The script should:

1. Use `huggingface_hub` to fetch the repository tree and normalize the output.
2. Emit a deterministic file manifest with the required properties.
3. Implement a retry mechanism with a backoff strategy.

Here's an example code snippet:
```bash
#!/usr/bin/env bash

# bin/snapshot.sh

# Generate deterministic file manifest for a dataset date folder.

# Usage:
# SNAPSHOT_ONLY=1 ./bin/snapshot.sh \
#   REPO=axentx/surrogate-1-training-pairs \
#   DATE_FOLDER=public-merged/2026-04-29 \
#   OUT_JSON=snapshots/2026-04-29/manifest.json

set -euo pipefail

: "${REPO:?required}"
: "${DATE_FOLDER:?required}"
: "${OUT_JSON:?required}"

OUT_DIR="$(dirname "${OUT_JSON}")"
mkdir -p "${OUT_DIR}"

# Fetch tree and normalize
RAW_JSON=$(huggingface-cli repo list-tree --repo "${REPO}" --path "${DATE_FOLDER}" --recursive=false)

# Use jq to normalize and sort deterministically
echo "${RAW_JSON}" | jq -c 'map({ path: .path, size: (.size // 0), sha: (.sha // null) }) | sort_by(.path)' > "${OUT_JSON}.tmp"

mv "${OUT_JSON}.tmp" "${OUT_JSON}"

echo "Snapshot written: ${OUT_JSON}"
```
**Conclusion:**

By synthesizing the best parts of multiple AI proposals, we can create a robust `bin/snapshot.sh` script that generates a deterministic file manifest for a dataset date folder. This approach eliminates the risk of API rate limits during data loading and ensures reliable ingestion. The provided code snippet can be refined and implemented to achieve this goal.
