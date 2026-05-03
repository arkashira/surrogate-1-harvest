# vanguard / quality

Based on the provided information, I will synthesize the best parts of the multiple AI proposals and combine the strongest insights into one final answer.

**Diagnosis:**

1. The absence of a persisted manifest per `(repo, dateFolder)` causes repeated authenticated HF API enumeration on every training launch, resulting in quota burn and risking 429 rate limits.
2. The frontend cannot pre-flight or cache available files, leading to users selecting invalid or mismatched paths, which causes training failures late in the process.
3. The training script likely uses `load_dataset(streaming=True)` on heterogeneous repositories, triggering PyArrow `CastError` on mixed-schema files.
4. Lightning Studio reuse is not implemented, resulting in new runs creating fresh studios and wasting 80+ hours of quota per month.
5. There is no CDN-only fallback for dataset fetches during training, causing authenticated API calls to continue inside Lightning workers.

**Proposed Solution:**

To address these issues, the following changes are proposed:

1. Create a manifest generation script (`/opt/axentx/vanguard/manifest.py`) that:
	* Generates a manifest file per `(repo, dateFolder)` using a single authenticated API call via `list_repo_tree`.
	* Embeds the manifest path in the training script.
	* Uses CDN-only URLs with zero authentication during data loading.
2. Modify the training script (`/opt/axentx/vanguard/train.py`) to:
	* Use the generated manifest file to load data.
	* Project the schema to `{prompt, response}` at parse time to avoid PyArrow `CastError`.
	* Reuse running Lightning Studio if present, and only start a new one if stopped or missing.
3. Implement a CDN-only fallback for dataset fetches during training to avoid authenticated API calls.

**Implementation:**

The implementation involves creating the manifest generation script (`/opt/axentx/vanguard/manifest.py`) and modifying the training script (`/opt/axentx/vanguard/train.py`) to use the generated manifest file and implement the proposed changes.

The manifest generation script will use the `HfApi` to list files in the specified `date_folder` and generate a manifest file containing the file names and CDN-only URLs. The training script will then use this manifest file to load data and project the schema to `{prompt, response}`.

The Lightning Studio reuse will be implemented by checking if a running studio with the same name exists, and if so, reusing it. If not, a new studio will be created.

**Code:**

The code for the manifest generation script (`/opt/axentx/vanguard/manifest.py`) and the modified training script (`/opt/axentx/vanguard/train.py`) is provided in the proposal.

**Conclusion:**

In conclusion, the proposed solution addresses the diagnosed issues by generating a persisted manifest per `(repo, dateFolder)`, using CDN-only URLs for data loading, projecting the schema to `{prompt, response}`, and reusing running Lightning Studio. The implementation involves creating a manifest generation script and modifying the training script to use the generated manifest file and implement the proposed changes.
