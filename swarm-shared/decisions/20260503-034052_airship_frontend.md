# airship / frontend

## Final Synthesis: CDN-Manifest + Idle-Resilient Launcher (Unified, Correct, Actionable)

**Core insight (merged):**  
Bypass HuggingFace API entirely during training by generating a static `manifest.json` for the date folder, embedding it in the training run, and letting Lightning workers fetch data from the CDN. Combine this with automatic detection + restart of idle/stopped Lightning Studio (L40S preferred, public tier fallback) while preserving progress. All changes are frontend-first and deployable in <2 hours.

**Resolved contradictions in favor of correctness + actionability:**
- **Manifest generation location:** Run once from the airship root (not inside Lightning) so it is deterministic, reproducible, and can be committed or uploaded to CDN.  
- **Manifest consumption:** Training script reads `/manifest.json` from the worker’s local filesystem or CDN; do not rely on HF API at runtime.  
- **Studio reuse vs. restart:** Prefer reusing an existing running studio; if stopped/idle, restart it (same machine) rather than always creating new. Fallback to public tier only if L40S unavailable (avoid surprise costs).  
- **Polling cadence:** 30s is acceptable for UI; do not hammer APIs. Use exponential backoff on repeated failures.  
- **Progress tracking:** Derive progress from logs or checkpoints if available; do not invent percentages. Show studio status truthfully.

---

## Implementation Plan (single, prioritized sequence)

### 1) One-time manifest generator (airship root)
File: `scripts/generate_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder (YYYY-MM-DD) listing dataset files
with size and sha256. Outputs to public/manifest.json for CDN/localhost use.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

def sha256_file(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(date_folder: str, dataset_root: Path, output_path: Path):
    target = dataset_root / date_folder
    if not target.is_dir():
        print(f"Folder not found: {target}", file=sys.stderr)
        sys.exit(1)

    entries = []
    for fpath in sorted(target.rglob("*")):
        if fpath.is_file():
            rel = fpath.relative_to(dataset_root)
            entries.append({
                "path": str(rel).replace("\\", "/"),
                "size": fpath.stat().st_size,
                "sha256": sha256_file(fpath)
            })

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_folder": date_folder,
        "files": entries
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(entries)} entries to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN manifest for dataset date folder")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder name")
    parser.add_argument("--dataset-root", default="data", help="Root containing date folders")
    parser.add_argument("--output", default="public/manifest.json", help="Output path")
    args = parser.parse_args()
    build_manifest(args.date, Path(args.dataset_root), Path(args.output))
```

Usage:
```bash
python3 scripts/generate_manifest.py --date 2026-05-03 --output public/manifest.json
# Upload/copy public/manifest.json to CDN or commit to repo so it's served at /manifest.json
```

---

### 2) Training script adaptation (Lightning worker)
Ensure the training script reads the manifest from a stable location (CDN or local file) and never calls HF API for file listing.

Example snippet (pseudo):
```python
import json
import os
import requests

def load_manifest():
    # Prefer local file; fallback to CDN if running in cloud worker
    local_path = "/manifest.json"
    cdn_url = os.getenv("MANIFEST_URL", "https://cdn.example.com/manifest.json")
    if os.path.exists(local_path):
        with open(local_path) as f:
            return json.load(f)
    else:
        resp = requests.get(cdn_url, timeout=10)
        resp.raise_for_status()
        return resp.json()

manifest = load_manifest()
file_urls = [f"https://cdn.example.com/{f['path']}" for f in manifest["files"]]
# Use file_urls to stream data; no HF API calls during training
```

---

### 3) Frontend Training Dashboard (React)
File: `frontend/src/components/TrainingDashboard.tsx`

Key behaviors:
- Load `/manifest.json` once (no HF API).
- Show manifest summary (file count, total size).
- Poll Lightning Studio state every 30s; auto-restart if idle/stopped (same machine).
- Prefer reusing an existing running studio; create new only if none running.
- Fallback to public tier only on L40S unavailability (configurable).
- Display real status; do not fake progress.

```tsx
import { useState, useEffect } from 'react';
import { LightningStudio } from '@lightningai/sdk';
import { Play, AlertCircle } from 'lucide-react';

interface ManifestEntry { path: string; size: number; sha256: string; }
interface TrainingStatus {
  studioId: string;
  status: 'running' | 'stopped' | 'idle' | 'error';
  lastHeartbeat: string;
}

export default function TrainingDashboard() {
  const [manifest, setManifest] = useState<ManifestEntry[]>([]);
  const [status, setStatus] = useState<TrainingStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load manifest (CDN bypass)
  useEffect(() => {
    fetch('/manifest.json')
      .then(r => r.json())
      .then(setManifest)
      .catch(() => setError('Failed to load manifest'));
  }, []);

  // Poll studio and auto-restart idle/stopped
  useEffect(() => {
    if (!status?.studioId) return;
    const interval = setInterval(async () => {
      try {
        const studio = await LightningStudio.get(status.studioId);
        const newStatus = studio.status as TrainingStatus['status'];
        setStatus(prev => prev ? { ...prev, status: newStatus, lastHeartbeat: new Date().toISOString() } : null);

        if (newStatus === 'stopped' || newStatus === 'idle') {
          // Restart same machine; fallback to public only if configured/unavailable
          await studio.start({ machine: 'L40S' }).catch(() => studio.start({ machine: 'public' }));
          setStatus(prev => prev ? { ...prev, status: 'running' } : null);
        }
      } catch {
        setError('Failed to poll studio');
      }
    }, 30000);
    return () => clearInterval(interval);
  }, [status?.studioId]);

  const resumeTraining = async () => {
    setLoading(true);
    setError(null);
    try {
      // Reuse running studio if exists
      const studios = await LightningStudio.list();
      let studio = studios.find(s => s.name === 'surrogate-training' && s.status === 'running');

      if (!studio) {
        studio = await LightningStudio.create({
          name: 'surrogate-training',
          machine: 'L40S',
          script: 'train.py',
          args: ['--manifest', '/manifest.json']
        });
      }

      setStatus({
        studioId: studio.id,
        status: studio.status as TrainingStatus['status'],
        lastHeartbeat: new Date().toISOString()
      });
    } catch (err: any) {
      setError(err?.message || 'Failed to start/resume training');
    } finally {
      setLoading(false);
    }
  };

  const totalSizeGB = (manifest.reduce((a
