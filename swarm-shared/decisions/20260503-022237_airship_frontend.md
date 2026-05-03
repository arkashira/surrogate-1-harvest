# airship / frontend

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, we need to analyze the provided information and identify the key elements that can be integrated to create a comprehensive solution.

**Key Elements:**

1. **CDN-first deterministic ingestion**: This involves generating a deterministic file list for a date folder in a HuggingFace dataset repo and using this list to fetch files from the CDN, bypassing the HF API and reducing the risk of hitting rate limits (429).
2. **Lightning Studio reuse + idle-stop guard**: This involves reusing existing Lightning studios with matching names and implementing a guard to restart stopped studios, ensuring that training jobs are not interrupted and reducing waste of Lightning quota.
3. **Arkship UI idle-stop resilience**: This involves adding auto-reconnect and status polling to the Surrogate AI service to prevent the UI from hanging when Lightning studios stop or restart.

**Synthesized Solution:**

To eliminate HF API 429 and Lightning quota waste during Surrogate training, we propose the following solution:

1. **Implement CDN-first deterministic ingestion**:
	* Generate a deterministic file list for each date folder in the HuggingFace dataset repo using a script like `list_date_folder.py`.
	* Embed the file list in the Lightning training container or mount it to ensure that `train.py` uses only CDN fetches with no Authorization header during data loading.
2. **Implement Lightning Studio reuse + idle-stop guard**:
	* Before running a training job, list existing Lightning studios with matching names and reuse any running studio.
	* Wrap each training job in a guard that checks the studio status and restarts the studio if it is stopped.
	* Persist small state (last repo/slug/date) to ensure deterministic resume.
3. **Implement Arkship UI idle-stop resilience**:
	* Add auto-reconnect and status polling to the Surrogate AI service to prevent the UI from hanging when Lightning studios stop or restart.
	* Expose a "Training status" indicator that survives backend restart.

**Code Snippets:**

The provided code snippets can be used as a starting point for implementing the synthesized solution. The `list_date_folder.py` script generates a deterministic file list, while the `train_wrapper.sh` script demonstrates how to reuse existing Lightning studios and implement an idle-stop guard. The `train.py` snippet shows how to use CDN-only fetches via the file list.

**Implementation Plan:**

To implement the synthesized solution, follow these steps:

1. Generate a deterministic file list for each date folder in the HuggingFace dataset repo.
2. Modify the `train.py` script to use CDN-only fetches via the file list.
3. Implement Lightning Studio reuse + idle-stop guard using a script like `train_wrapper.sh`.
4. Add auto-reconnect and status polling to the Surrogate AI service to prevent UI hangs.
5. Expose a "Training status" indicator that survives backend restart.

By following this implementation plan, you can eliminate HF API 429 and Lightning quota waste during Surrogate training, ensuring a more efficient and reliable training process.
