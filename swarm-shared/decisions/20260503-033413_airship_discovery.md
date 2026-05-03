# airship / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and resolve any contradictions in favor of correctness and concrete actionability.

### Objective
The primary objective is to make Surrogate training **HF-rate-limit-proof and Lightning-idle-resilient**. This involves creating a system that can withstand rate limits imposed by Hugging Face (HF) and resiliently handle idle stops in Lightning, a platform for AI model training.

### Implementation Plan
The proposed implementation plan involves several steps:

1. **Add a CDN-only manifest generator**: This step is crucial for bypassing HF rate limits. By generating a manifest that lists files and their corresponding CDN URLs, the training process can directly access files from the CDN without hitting HF API rate limits.
2. **Add a Lightning-aware training launcher**: This launcher should reuse running studios (if available) or start a new one (with a fallback to a free tier if necessary) and run the training script with idle-resilient retry logic. This ensures that training can continue even if the studio stops due to idle time.
3. **Modify the training script to consume the manifest**: The training script needs to be updated to use the generated manifest for streaming files via CDN, ensuring zero HF API calls during training.
4. **Implement health checks and restart mechanisms**: Adding a health check script and configuring it with systemd or cron to restart the idle studio before each run ensures continuous training.
5. **Update documentation**: Finally, updating the README with one-line usage and a note about bypassing rate limits makes the solution user-friendly.

### Code Snippets
The provided code snippets demonstrate how to implement these steps:

- **`make_cdn_manifest.py`**: Generates a CDN-only manifest for a Surrogate training date-folder. It lists files in a specified date folder on HF, constructs CDN URLs, and writes them to a `manifest.json` file.
- **`lightning_train.py`**: A Lightning-aware launcher that reuses or starts a studio, runs the training script with idle-resilient retry, and handles studio stops.
- **`train_cdn.py`**: An example loader that consumes the `manifest.json` and streams files via CDN for training, minimizing HF API calls.

### Synthesis and Actionability
To synthesize the information and ensure correctness and actionability:

1. **Ensure Correctness**: Verify that the CDN manifest generation correctly lists files and their CDN URLs, and that the training launcher properly handles studio restarts and idle stops.
2. **Implement Idle-Resilient Logic**: Incorporate retry mechanisms in the training launcher to handle studio stops due to idle time, ensuring training continuity.
3. **Streamline Documentation**: Update the README to include clear instructions on generating the manifest, running the training launcher, and any specific configurations needed for Lightning and HF.
4. **Test End-to-End**: Perform end-to-end testing of the entire workflow, from manifest generation to training completion, to ensure that all components work together seamlessly.

By following these steps and ensuring that each component is correctly implemented and tested, the Surrogate training process can be made both HF-rate-limit-proof and Lightning-idle-resilient, significantly improving its reliability and efficiency.
