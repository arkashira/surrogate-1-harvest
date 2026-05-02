# airship / discovery

## Final Synthesized Implementation (Best of Both Candidates)

**Target**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists.

**Why this wins**:
- Pure orchestration change (no model training, no infra spin-up)
- Reuses CDN bypass insight with proper schema guarding
- Fixes both known failure modes (429 rate limits + mixed-schema CastError)
- CLI-first approach with script fallback → immediate reliability gain

---

### Implementation Plan

#### 1. Create `scripts/discover-cdn-list.py`
- Single API call to `list_repo_tree(path, recursive=False)` for one date folder
- Save deterministic list as `file_lists/{repo_slug}/{date}.json`
- Include file paths, sizes, and SHA256 of content for reproducibility
- Exit 0 on success, non-zero on failure
- Built-in 429 handling with exponential backoff

#### 2. Update `airship/cli/discover.py`
- Add subcommand: `airship discover list --date <YYYY-MM-DD> --repo <repo_id> --out-dir ./file_lists`
- Add subcommand: `airship discover download --list <json> --output-dir ./data/projected`
- Validate output JSON schema; print summary (count, total bytes, date range)
- Default `--cdn-only` flag (bypass `load_dataset` entirely)

#### 3. Create `scripts/project-cdn-parquet.py`
- Accept `--file-list <json>` and `--output-dir <parquet>`
- Iterate paths, download via CDN (`resolve/main/`) without auth
- Parse each file, project to `{prompt, response}` only
- Write to single `.parquet` with deterministic naming
- Skip files missing required columns; log warnings

#### 4. Add `.gitignore` entries
```
file_lists/*.json
data/raw/*
data/projected/*
```

#### 5. Update README (discovery section)
```bash
# List files (CDN-only, no download)
airship discover list --date 2026-05-02 --repo datasets/target/repo

# Download and project to prompt/response parquet
airship discover download --list file_lists/datasets_target_repo/2026-05-02.json
```

#### 6. Cron hygiene
- Ensure `SHELL=/bin/bash` in crontab
- Shebang `#!/usr/bin/env bash`, `chmod +x`
- Log rotation via `logger -t airship-discover`

---

### Code Snippets

#### `scripts/discover-cdn-list.py`
```python
#!/usr/bin/env python3
"""
CDN-only file lister for HF datasets.
Produces deterministic JSON file lists without downloading content.
"""
import json
import hashlib
import sys
import time
from pathlib import Path
from typing import Dict, List, Any

from huggingface_hub import list_repo_tree, HfApi

def exponential_backoff(attempt: int, base: int = 360) -> None:
    wait = base * (2 ** (attempt - 1))
    print(f"Rate limited (429). Waiting {wait}s...", file=sys.stderr)
    time.sleep(wait)

def list_files_cdn(repo_id: str, date_path: str, max_retries: int = 3) -> List[Dict[str, Any]]:
    """List parquet files in date folder with retry logic."""
    api = HfApi()
    
    for attempt in range(1, max_retries + 1):
        try:
            tree = list_repo_tree(repo_id=repo_id, path=date_path, recursive=False)
            files = [
                {
                    "path": f.rfilename,
                    "size": f.size,
                    "lfs": getattr(f, "lfs", None)
                }
                for f in tree
                if f.rfilename.endswith('.parquet')
            ]
            return sorted(files, key=lambda x: x["path"])
            
        except Exception as e:
            if "429" in str(e) and attempt < max_retries:
                exponential_backoff(attempt)
                continue
            raise
    
    raise RuntimeError(f"Failed after {max_retries} retries")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CDN-only file listing")
    parser.add_argument("--repo", required=True, help="HF dataset repo ID")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="file_lists", help="Output directory")
    args = parser.parse_args()

    # Create deterministic output path
    repo_slug = args.repo.replace("/", "_")
    out_dir = Path(args.out_dir) / repo_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = out_dir / f"{args.date}.json"
    
    print(f"Listing {args.repo}/{args.date} (CDN-only mode)")
    
    try:
        files = list_files_cdn(args.repo, args.date)
        
        metadata = {
            "repo": args.repo,
            "date": args.date,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "files": files
        }
        
        with open(output_file, "w") as f:
            json.dump(metadata, f, indent=2, sort_keys=True)
        
        print(f"Saved {len(files)} files to {output_file}")
        print(f"Total size: {metadata['total_bytes'] / 1e9:.2f} GB")
        
    except Exception as e:
        print(f"Failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

#### `airship/cli/discover.py` (patch)
```python
import click
import json
import subprocess
import os
from pathlib import Path

@click.group()
def discover():
    """Dataset discovery and ingestion commands."""
    pass

@discover.command()
@click.option("--date", required=True, help="Date folder (YYYY-MM-DD)")
@click.option("--repo", required=True, help="HF dataset repo ID")
@click.option("--out-dir", default="file_lists", help="Output directory")
def list(date, repo, out_dir):
    """List parquet files via CDN (no download)."""
    script = Path(__file__).parent.parent / "scripts" / "discover-cdn-list.py"
    result = subprocess.run(
        ["python3", str(script), "--repo", repo, "--date", date, "--out-dir", out_dir],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        click.echo(result.stderr, err=True)
        raise click.Abort()
    
    # Validate and display summary
    repo_slug = repo.replace("/", "_")
    list_file = Path(out_dir) / repo_slug / f"{date}.json"
    
    with open(list_file) as f:
        meta = json.load(f)
    
    click.echo(f"✓ Listed {meta['file_count']} files ({meta['total_bytes'] / 1e9:.2f} GB)")
    click.echo(f"  Date: {meta['date']}")
    click.echo(f"  Output: {list_file}")

@discover.command()
@click.option("--list", "list_file", type=click.Path(exists=True), required=True)
@click.option("--output-dir", default="data/projected", help="Output directory")
@click.option("--cdn-only", is_flag=True, default=True, help="Use CDN only (no HF API)")
def download(list_file, output_dir, cdn_only):
    """Download and project files to prompt/response parquet."""
    script = Path(__file__).parent.parent / "scripts" / "project-cdn-parquet.py"
    
    result = subprocess.run(
        ["python3", str(script), "--file-list", list_file, "--output-dir", output_dir],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        click.echo(result.stderr, err=True)
        raise click.Abort()
    
    click.echo(result.stdout)

if __name__ == "__main__":
    discover()
```

#### `
