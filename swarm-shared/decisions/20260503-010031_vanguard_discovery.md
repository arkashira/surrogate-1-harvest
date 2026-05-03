# vanguard / discovery

Based on the provided information, I will synthesize the best parts of the proposals and provide a final answer that combines the strongest insights and resolves contradictions in favor of correctness and concrete actionability.

**Diagnosis and Proposed Change:**

The main issues identified are:

1. No persisted `(repo, dateFolder)` file manifest, leading to repeated enumeration via authenticated HF API and quota burning.
2. Recursive enumeration and pagination amplify rate-limit pressure and expose mixed-schema files unnecessarily.
3. Training script relies on `load_dataset(streaming=True)` on heterogeneous repos, triggering `pyarrow.CastError` on schema drift.
4. Lightning Studio reuse is not enforced, leading to idle-stop kills and wasted quota on repeated studio creation.
5. No CDN-only data path; authenticated API calls continue during data loading instead of using public CDN URLs.

To address these issues, the proposed change involves creating a script to generate a persisted manifest file and updating the training script to use this manifest and CDN URLs for data loading.

**Implementation:**

The implementation involves creating a new script, `build_manifest.py`, which generates a persisted manifest file containing the repository, date, and file information. This script will be used to build the manifest file once per date folder.

The training script, `run_surrogate.py`, will be updated to use the generated manifest file and CDN URLs for data loading. This update will involve loading the manifest file, using the CDN URLs to fetch the data, and projecting the data to the required format at parse time.

**Code:**

The code for the `build_manifest.py` script is provided, which generates the persisted manifest file. The updated `run_surrogate.py` script is also provided, which uses the generated manifest file and CDN URLs for data loading.

**Verification:**

To verify the implementation, the following steps can be taken:

1. Build the manifest file using the `build_manifest.py` script.
2. Validate the CDN-only loading by using the `requests` library to fetch the data from the CDN URL.
3. Run the updated `run_surrogate.py` script to verify that it uses the generated manifest file and CDN URLs for data loading.

**Final Answer:**

The final answer is to implement the proposed change by creating a script to generate a persisted manifest file and updating the training script to use this manifest and CDN URLs for data loading. This implementation will address the identified issues and provide a more efficient and scalable solution for data loading and training.

Here is the combined code:
```python
# build_manifest.py
import argparse
import json
import os
import sys
from pathlib import Path
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser(description="Build CDN manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out-dir", default="manifests", help="Output directory (relative to project root)")
    args = parser.parse_args()

    api = HfApi()
    root = Path(__file__).parent.parent.parent
    out_dir = root / args.out_dir / args.repo.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.date}.json"

    try:
        tree = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)
        files = []
        for entry in tree:
            if hasattr(entry, "path"):
                p = entry.path
                size = getattr(entry, "size", None)
            else:
                p = entry.get("path")
                size = entry.get("size")
            if p:
                files.append({"path": p, "size": size})
    except Exception as exc:
        print(f"Failed to list repo tree: {exc}")
        sys.exit(1)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()

# run_surrogate.py
import os
import json
import requests
from pathlib import Path
from lightning.pytorch import seed_everything, Trainer
from lightning.pytorch.studio import Studio, Teamspace
from datasets import load_dataset, Dataset

def get_or_create_studio(name: str = "surrogate-train"):
    team = Teamspace()
    for s in team.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating new studio: {name}")
    return Studio(create_ok=True, name=name)

def load_manifest(repo: str, date: str):
    root = Path(__file__).parent.parent.parent
    manifest_path = root / "manifests" / repo.replace("/", "_") / f"{date}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    with open(manifest_path) as f:
        return json.load(f)

def cdn_dataset_generator(manifest):
    base = f"https://huggingface.co/datasets/{manifest['repo']}/resolve/main"
    for item in manifest["files"]:
        url = f"{base}/{item['path']}"
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            obj = json.loads(line)
            yield {"prompt": obj.get("prompt", ""), "response": obj.get("response", "")}

def main():
    seed_everything(42, workers=True)
    manifest = load_manifest(repo="my-org/datasets", date="2026-04-29")
    ds = Dataset.from_generator(cdn_dataset_generator, gen_kwargs={"manifest": manifest})
    ds = ds.train_test_split(test_size=0.05)
    studio = get_or_create_studio("surrogate-train")
    trainer = Trainer(max_epochs=1, accelerator="gpu", devices=1)
    print("Run setup complete (manifest + CDN + studio reuse).")

if __name__ == "__main__":
    main()
```
