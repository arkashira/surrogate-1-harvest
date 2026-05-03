# airship / frontend

# Final synthesized plan (airship/frontend + Lightning)

**Goal (highest value, <2h):**  
Embed a CDN-only file manifest into the Surrogate training frontend/launcher so Lightning jobs fetch parquet shards via HuggingFace CDN (bypassing HF API rate limits) and auto-recover from idle timeouts — with zero schema/infra changes.

---

## Concrete implementation (unified, ready to run)

### 1) Generate CDN manifest (Mac orchestrator) — 20 min

`/opt/axentx/airship/scripts/prepare_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for a HuggingFace dataset repo.
Run after HF API window clears. Outputs shared/manifest.json.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "my-org/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/opt/axentx/airship/shared"))
OUTPUT_FILE = OUTPUT_DIR / "manifest.json"

def build_manifest() -> dict:
    api = HfApi()
    # Non-recursive call(s) to avoid heavy pagination/rate limits
    entries = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)

    files = []
    for entry in entries:
        if entry.path.endswith(".parquet"):
            cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{entry.path}"
            files.append(
                {
                    "path": entry.path,
                    "cdn_url": cdn_url,
                    "size": getattr(entry, "size", 0),
                }
            )

    manifest = {
        "repo": HF_REPO,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "date_folder": DATE_FOLDER,
        "files": files,
    }
    return manifest

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    OUTPUT_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUTPUT_FILE} ({len(manifest['files'])} files)")

if __name__ == "__main__":
    main()
```

Make executable and run:

```bash
chmod +x /opt/axentx/airship/scripts/prepare_cdn_manifest.py

HF_DATASET_REPO=my-org/surrogate-dataset \
DATE_FOLDER=2026-04-29 \
OUTPUT_DIR=/opt/axentx/airship/shared \
python /opt/axentx/airship/scripts/prepare_cdn_manifest.py
```

---

### 2) Frontend: manifest provider (React) — 30 min

`/opt/axentx/airship/frontend/src/context/FileManifestProvider.tsx`

```tsx
import React, { createContext, useContext, useEffect, useState } from 'react';

type ManifestEntry = {
  path: string;
  cdn_url: string;
  size: number;
  sha256?: string;
};

type FileManifest = {
  repo: string;
  created_at: string;
  date_folder: string;
  files: ManifestEntry[];
};

const ManifestContext = createContext<{
  manifest: FileManifest | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}>({
  manifest: null,
  loading: false,
  error: null,
  refresh: async () => {},
});

export const FileManifestProvider: React.FC<{ repo: string; children: React.ReactNode }> = ({
  repo,
  children,
}) => {
  const [manifest, setManifest] = useState<FileManifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/manifest?repo=${encodeURIComponent(repo)}`);
      if (!res.ok) throw new Error(`Failed to fetch manifest: ${res.status}`);
      const data: FileManifest = await res.json();
      setManifest(data);
    } catch (err: any) {
      setError(err.message || 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, [repo]);

  return (
    <ManifestContext.Provider value={{ manifest, loading, error, refresh }}>
      {children}
    </ManifestContext.Provider>
  );
};

export const useFileManifest = () => useContext(ManifestContext);
```

Backend endpoint to serve generated manifest (Node/Express):

`/opt/axentx/airship/frontend/src/api/manifest.ts`

```ts
import express from 'express';
import fs from 'fs';
import path from 'path';

const router = express.Router();

router.get('/manifest', (req, res) => {
  const repo = req.query.repo as string;
  if (!repo) return res.status(400).json({ error: 'repo required' });

  const manifestPath = path.resolve(process.cwd(), '../../shared/manifest.json');
  if (!fs.existsSync(manifestPath)) return res.status(404).json({ error: 'manifest not found' });

  const raw = fs.readFileSync(manifestPath, 'utf8');
  const manifest = JSON.parse(raw);
  res.json(manifest);
});

export default router;
```

---

### 3) Lightning launcher: reuse + idle-resilient wrapper — 30 min

`/opt/axentx/airship/scripts/run_lightning_studio.py`

```python
#!/usr/bin/env python3
"""
Launch (or reuse) a Lightning Studio and run train.py with idle-resilient wrapper.
Keeps process alive and restarts training step on idle timeout.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import lightning as L

MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", "/opt/axentx/airship/shared/manifest.json"))
TRAIN_SCRIPT = Path(os.getenv("TRAIN_SCRIPT", "/opt/axentx/airship/train.py"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
HEALTH_INTERVAL = int(os.getenv("HEALTH_INTERVAL", "60"))  # seconds

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())

def run_training_with_heartbeat() -> None:
    """
    Run train.py in a subprocess. If it exits non-zero or is idle too long,
    restart up to MAX_RETRIES.
    """
    retries = 0
    while retries <= MAX_RETRIES:
        print(f"[run_lightning_studio] Starting training (attempt {retries + 1}/{MAX_RETRIES + 1})")
        proc = subprocess.Popen(
            [sys.executable, str(TRAIN_SCRIPT), "--manifest", str(MANIFEST_PATH)],
            env={**os.environ, "HF_DATASET_REPO": load_manifest()["repo"]},
        )

        last_output = time.time()
        try:
            while proc.poll() is None:
                time.sleep(5)
                # Simple liveness: if subprocess produces no output for HEALTH_INTERVAL, treat as idle
                # (In practice, hook into Lightning's status or logs.)
                if time.time() - last_output > HEALTH_INTERVAL:
                    print("[run_lightning_studio] No recent output — possible idle timeout. Restarting step.")
                    proc.terminate()
                    proc.wait(timeout=30)
                    break
                # You can improve this by tailing logs or checking Lightning status.
        except Exception as exc:
            print(f"[run_lightning_studio] Error while monitoring: {exc}")
            proc.terminate()

        rc
