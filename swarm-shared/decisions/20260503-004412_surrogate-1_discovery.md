# surrogate-1 / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and resolve any contradictions in favor of correctness and concrete actionability.

### Overview of the Proposals

Both proposals suggest implementing a Mac-side tool, `tools/snapshot_manifest.py`, which lists one date-partition via a single Hugging Face (HF) API call. This tool emits a `file_manifest.json` containing CDN URLs and is accompanied by a training script that utilizes CDN-only fetches. The primary goal is to apply the HF CDN bypass pattern, avoiding 429/rate-limit errors during training while keeping data ingestion simple.

### Key Components and Steps

1. **Create `tools/snapshot_manifest.py`**: This script performs a single `list_repo_tree` call for a specified date folder (e.g., `batches/public-merged/2026-05-03`), generates a `file_manifest.json` with CDN URLs, and includes deterministic ordering for consistency across Lightning workers.

2. **Develop `tools/train_cdn_loader.py`**: This script reads the `file_manifest.json`, builds an `IterableDataset` that downloads data via CDN URLs using `requests`/`urllib` and `pyarrow`, projects only `{prompt, response}` at parse time, and implements retry/backoff on CDN failures.

3. **Update README**: Include instructions for creating a CDN manifest and using it for training in Lightning Studio, emphasizing the avoidance of HF API calls and quota churn.

4. **Smoke Test**: Perform a test run against a small date folder to verify manifest correctness and the loader's ability to stream and project rows without HF API authentication.

### Synthesized Final Answer

The most effective approach involves:

- **Implementing `tools/snapshot_manifest.py`** to generate a `file_manifest.json` with CDN URLs for a specified date partition. This step ensures that only one HF API call is made, reducing the risk of rate limiting.

- **Developing `tools/train_cdn_loader.py`** to facilitate CDN-only data loading. This script should handle different file types (parquet, jsonl), project relevant fields (`prompt`, `response`), and include a retry mechanism for CDN failures.

- **Updating the README** to provide clear instructions on using the `snapshot_manifest.py` tool and integrating the `train_cdn_loader.py` script into the training process in Lightning Studio. This documentation should highlight the benefits of this approach, including the avoidance of HF API rate limits and simplified data ingestion.

- **Conducting a thorough smoke test** to validate the functionality of both scripts and ensure that the generated manifest is correct and usable for training without encountering HF API authentication issues.

By following this synthesized approach, you can effectively bypass HF API rate limits during training, streamline your data loading process, and ensure a more reliable and efficient training workflow. 

Here is a code snippet that combines the key elements:
```python
# tools/snapshot_manifest.py
import argparse
import json
from huggingface_hub import HfApi

def list_date_partition(date_folder, token):
    api = HfApi(token=token)
    entries = api.list_repo_tree(repo_id="axentx/surrogate-1-training-pairs", path=date_folder, recursive=False)
    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        path = entry.path
        files.append({
            "path": path,
            "cdn_url": f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}",
            "size": getattr(entry, "size", None),
        })
    files.sort(key=lambda f: f["path"])
    return files

def main():
    parser = argparse.ArgumentParser(description="Create CDN manifest for a date partition.")
    parser.add_argument("--date", required=True, help="Date folder, e.g., batches/public-merged/2026-05-03")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()
    token = "YOUR_HF_TOKEN"
    files = list_date_partition(args.date, token)
    manifest = {
        "date_folder": args.date,
        "files": files,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

if __name__ == "__main__":
    main()
```

```python
# tools/train_cdn_loader.py
import json
import requests
from pathlib import Path
from typing import Iterator, Dict

def download_cdn(url: str, dest: Path) -> Path:
    resp = requests.get(url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest

def project_row(raw: Dict[str, str]) -> Dict[str, str]:
    return {
        "prompt": raw.get("prompt", ""),
        "response": raw.get("response", ""),
    }

def stream_manifest(manifest_path: Path) -> Iterator[Dict[str, str]]:
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        url = entry["cdn_url"]
        suffix = Path(url).suffix.lower()
        with Path(tempfile.mkdtemp()) / f"file{suffix}" as p:
            download_cdn(url, p)
            if suffix == ".parquet":
                # Handle parquet files
                pass
            elif suffix in {".jsonl", ".json"}:
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    yield project_row(row)

# Example usage
manifest_path = Path("file_manifest.json")
for row in stream_manifest(manifest_path):
    # Process row
    pass
```
