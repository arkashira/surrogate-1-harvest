# surrogate-1 / frontend

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and identify key points.

**Key Points:**

1. **Deterministic Pre-Flight File Listing**: Both proposals suggest adding a script (`bin/list-files.py`) to generate a deterministic file list for a HuggingFace dataset repo. This list includes file paths, sizes, and SHA256 hashes.
2. **CDN-Only Ingestion**: Both proposals recommend using CDN-only ingestion to eliminate HF API 429 errors during training and make shard workers resilient. This involves updating `bin/dataset-enrich.sh` to accept an optional file list and using CDN URLs for data loading.
3. **Updated `bin/dataset-enrich.sh`**: The first proposal provides an updated version of `bin/dataset-enrich.sh` that accepts an optional `--file-list` argument. If provided, the script uses CDN-only fetches and avoids mixed-schema pyarrow errors.
4. **Requirements Updates**: The second proposal suggests adding `requirements-cdn.txt` (with requests and tenacity) and updating `requirements.txt` to pin versions.

**Synthesized Final Answer:**

To improve the implementation plan, we will:

1. **Add `bin/list-files.py`**: Create a one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256). This single API call happens after any rate-limit window clears.
2. **Update `bin/dataset-enrich.sh`**: Modify the script to accept an optional `--file-list` argument. If provided, use CDN-only fetches and avoid mixed-schema pyarrow errors.
3. **Add `requirements-cdn.txt`**: Create a new requirements file with requests and tenacity.
4. **Update `requirements.txt`**: Pin versions to ensure consistency.

**Concrete Actionability:**

To implement these changes, follow these steps:

1. Create `bin/list-files.py` with the provided Python code.
2. Update `bin/dataset-enrich.sh` with the provided Bash code.
3. Add `requirements-cdn.txt` with the required packages (requests and tenacity).
4. Update `requirements.txt` to pin versions.
5. Run `bin/list-files.py` to generate the file list.
6. Use the generated file list with `bin/dataset-enrich.sh` to enable CDN-only ingestion.

By following these steps, you can improve the implementation plan and eliminate HF API 429 errors during training, making shard workers more resilient.
