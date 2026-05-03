# surrogate-1 / frontend

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's break down the key components and actions proposed by the candidates:

1. **Pre-flight Snapshot + CDN-bypass Pattern**: Both candidates emphasize the importance of implementing a pre-flight snapshot and CDN-bypass pattern for surrogate-1 training. This approach removes HF API calls during training, bypasses 429 rate limits, and enables deterministic, reproducible dataset slices for Lightning Studio runs.

2. **Implementation Plan**: The plan involves creating a `bin/snapshot.sh` script that uses the HF API once to list the repository tree for a specific date folder, producing a `snapshot/<date>/filelist.json` file. This file contains a deterministic, sorted list of files with their CDN URLs.

3. **Modify `bin/dataset-enrich.sh`**: The script needs to be updated to accept an optional `SNAPSHOT_FILE` environment variable. If provided, it reads the file list and streams data via CDN URLs, bypassing the HF API during worker runs. The existing `load_dataset` fallback is kept for local/dev runs.

4. **Add Lightweight `bin/train.py` Stub**: A small `bin/train.py` script is proposed to load the `snapshot/<date>/filelist.json`, stream parquet files from the CDN, project to `{prompt, response}` only, and output `batches/mirror-merged/{date}/{slug}.parquet` without extra columns.

5. **No Schema Changes, Secrets, or Infra**: The proposed solution does not require schema changes, new secrets, or infrastructure adjustments, making it a lightweight and efficient improvement.

**Synthesized Final Answer**:

To enhance the training process, implement a pre-flight snapshot and CDN-bypass pattern. This involves:

- Creating a `bin/snapshot.sh` script to generate a deterministic snapshot of the dataset files with their CDN URLs.
- Updating `bin/dataset-enrich.sh` to use the snapshot file for CDN-only streaming, reducing reliance on the HF API.
- Adding a lightweight `bin/train.py` script for streaming parquet files from the CDN and projecting the data to the required format.

**Estimated Implementation Time**: Less than 2 hours.

**Benefits**:
- Bypasses HF API rate limits during training.
- Enables deterministic and reproducible dataset slices.
- Improves training reliability and efficiency without requiring significant infrastructure or schema changes.

By following this synthesized approach, you can efficiently implement a high-value incremental improvement to your training process, enhancing reliability and reducing the impact of rate limits on your workflow.
