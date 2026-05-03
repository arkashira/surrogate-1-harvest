# vanguard / frontend

Based on the provided AI proposals, I will synthesize the best parts and combine the strongest insights into a final answer. 

The primary issue is that the frontend still uses authenticated HF API calls for every preview/training launch, which burns quota and risks 429s. To resolve this, we need to implement a CDN-bypass data loader and a frontend manifest cache.

**Proposed Solution:**

1. **Create a frontend manifest cache**: Generate a static list of dataset files for the current date folder using an orchestration script. Commit/update this list using CI and store it in `src/frontend/data/file-manifest.json`.
2. **Implement a CDN-bypass data loader**: Create `src/frontend/lib/hf-cdn.ts` with two main functions:
	* `getDatasetFileList()`: Reads the embedded JSON manifest and returns the list of dataset files.
	* `fetchViaCDN(repo, path)`: Bypasses the API and fetches data from the CDN using public URLs (`https://huggingface.co/datasets/.../resolve/main/...`).
3. **Update preview/training launch components**: Use the CDN loader and local manifest instead of calling `list_repo_tree` and `load_dataset` APIs.
4. **Add request deduplication and retry/backoff for CDN fetches**: Implement a mechanism to deduplicate requests and retry failed fetches with a backoff strategy to ensure a robust UX.
5. **Implement a studio reuse mechanism**: Create a utility to list and reuse running Lightning Studio instances before creating new ones.
6. **Add a status-check and restart guard**: Implement a status check and restart guard before launching runs to prevent idle-stop killing training.

**Implementation:**

The implementation will involve creating the necessary files and functions, updating the frontend code to use the CDN loader and local manifest, and adding the studio reuse and status-check mechanisms.

**Files to touch:**

* `src/frontend/lib/hf-cdn.ts`: CDN URL builder and manifest loader
* `src/frontend/data/file-manifest.json`: Static list of dataset files
* `src/frontend/pages/training.tsx`: Update to use CDN loader and local manifest
* `src/frontend/lib/studio.ts`: Studio reuse and idle-stop guard

**Verification:**

To verify the implementation, we can perform the following tests:

1. Load the training page and confirm that the file list populates and all URLs are CDN URLs.
2. Open browser devtools Network tab and verify that no authenticated HF API calls are made during manifest load or file enumeration.
3. Click “Start Training” and confirm that it finds or creates a running studio and calls the safe run function.
4. Check Lightning Teamspace UI and confirm studio reuse (no duplicate studios created on repeated clicks).

By implementing these changes, we can resolve the contradictions and ensure correctness and concrete actionability. The proposed solution addresses the primary issues and provides a robust and efficient way to load data and manage studios.
