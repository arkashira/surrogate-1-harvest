# airship / frontend

## Airship Frontend — Highest-Value Incremental (<2h)

## Chosen improvement
Add a lightweight frontend page (`/discover`) that shows the latest CDN-only manifest produced by `airship discover` and exposes a “Copy CDN train script” button.  
This unblocks immediate surrogate-1 training iterations by giving users a zero-API, CDN-only training bootstrap without waiting for full backend plumbing.

## Implementation plan (≤2h)
1. Add `/discover` route + page (Next.js App Router)  
   - Static generation preferred; accepts `repo`/`date` query params to hydrate manifest URL.
2. Add `airship discover` CLI (Python)  
   - Single `list_repo_tree` call → save `manifest-{date}.json` to `public/manifests/`.
   - Embed file list + repo into JSON; include generated CDN train script snippet.
3. Frontend fetches `/manifests/manifest-{date}.json` (CDN path, no auth) and renders:
   - File count + total size
   - Copy-to-clipboard button for train script
   - Raw JSON viewer (collapsible)
4. Add convenience npm script: `npm run discover -- --repo oxbot/surrogate-data --date 2026-05-01`

## Code snippets

### 1) CLI: `scripts/airship_discover.py`
```python
#!/usr/bin/env python3
"""
airship discover
Generate CDN-only manifest for a HuggingFace dataset repo folder.
Usage:
  python scripts/airship_discover.py --repo oxbot/surrogate-data --date 2026-05-01
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def build_manifest(repo_id: str, date_folder: str, out_dir: Path):
    api = HfApi()
    # Single API call (rate-limited); list only top-level of date folder
    tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)

    files = []
    total_size = 0
    for item in tree:
        if item.type != "file":
            continue
        # CDN URL (no auth, bypasses /api/ rate limits)
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{date_folder}/{item.path}"
        files.append({
            "path": item.path,
            "size": item.size,
            "cdn_url": cdn_url,
        })
        total_size += item.size if item.size else 0

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "file_count": len(files),
        "total_size": total_size,
        "files": files,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"manifest-{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))

    # Also produce a ready-to-run CDN-only training script snippet
    train_snippet = f"""# CDN-only surrogate-1 training (Lightning)
# Uses manifest produced by airship discover — zero HF API calls during training
import json
from pathlib import Path
from torch.utils.data import Dataset, DataLoader
import requests

MANIFEST_PATH = Path("manifests/manifest-{date_folder}.json")
OUT_DIR = Path("data/{date_folder}")
OUT_DIR.mkdir(parents=True, exist_ok=True)

with MANIFEST_PATH.open() as f:
    manifest = json.load(f)

class CDNParquetDataset(Dataset):
    def __init__(self, files):
        self.files = files  # list of dict with cdn_url

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        url = self.files[idx]["cdn_url"]
        # stream download; project to (prompt, response) at parse time
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        # TODO: parse parquet -> (prompt, response)
        return {{"raw_bytes": r.content, "url": url}}

dataset = CDNParquetDataset(manifest["files"])
loader = DataLoader(dataset, batch_size=8, shuffle=False, num_workers=4)
print("CDN dataset ready — file_count:", manifest["file_count"], "total_size:", manifest["total_size"])
"""
    snippet_path = out_dir / f"train-cdn-{date_folder}.py"
    snippet_path.write_text(train_snippet)

    print(f"Manifest written: {out_path}")
    print(f"Train snippet: {snippet_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="airship discover — CDN-only manifest")
    parser.add_argument("--repo", required=True, help="HF repo id (e.g. oxbot/surrogate-data)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-01)")
    parser.add_argument("--out", default="public/manifests", help="Output directory (relative to project root)")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    out_dir = project_root / args.out
    build_manifest(args.repo, args.date, out_dir)
```

Make executable:
```bash
chmod +x scripts/airship_discover.py
```

### 2) Frontend: `app/discover/page.tsx`
```tsx
'use client';

import { useState, useEffect } from 'react';

export default function DiscoverPage() {
  const [repo, setRepo] = useState('oxbot/surrogate-data');
  const [date, setDate] = useState('2026-05-01');
  const [manifest, setManifest] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadManifest() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/manifests/manifest-${date}.json`, { cache: 'no-store' });
      if (!res.ok) throw new Error('Manifest not found. Run airship discover first.');
      const data = await res.json();
      setManifest(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadManifest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const trainScript = `# CDN-only surrogate-1 training (Lightning)
# Uses manifest produced by airship discover — zero HF API calls during training
import json
from pathlib import Path
from torch.utils.data import Dataset, DataLoader
import requests

MANIFEST_PATH = Path("manifests/manifest-${date}.json")
OUT_DIR = Path("data/${date}")
OUT_DIR.mkdir(parents=True, exist_ok=True)

with MANIFEST_PATH.open() as f:
    manifest = json.load(f)

class CDNParquetDataset(Dataset):
    def __init__(self, files):
        self.files = files

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        url = self.files[idx]["cdn_url"]
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return {"raw_bytes": r.content, "url": url}

dataset = CDNParquetDataset(manifest["files"])
loader = DataLoader(dataset, batch_size=8, shuffle=False, num_workers=4)
print("CDN dataset ready — file_count:", manifest["file_count"], "total_size:", manifest["total_size"])
`;

  async function copyScript() {
    await navigator.clipboard.writeText(trainScript);
    alert('Train script copied to clipboard');
  }

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">Discover — CDN Manifest</h1>

      <div className="flex gap-2 mb-4
