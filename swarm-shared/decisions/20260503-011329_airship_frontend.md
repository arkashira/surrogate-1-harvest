# airship / frontend

# Final Synthesis — Airship Frontend + Training Launcher (HF CDN Bypass + Studio Reuse)

## Core goal
- Let the Airship/Arkship frontend trigger surrogate-1 training without HF API 429s or `pyarrow.CastError`, and avoid burning Lightning quota by reusing running Studios.
- Pure additive changes; no backend required unless you want a proxy route.
- Ship in ≤2 hours.

---

## Chosen approach (resolve contradictions)
- **Correctness**: Use CDN-only fetches (no Authorization headers) and explicit schema projection (`{prompt, response}`) to eliminate `pyarrow.CastError`.  
- **Actionability**: Provide a CLI-first workflow (manifest + reuse guard) that the frontend can invoke either via a backend route or locally via SSE/streaming logs.  
- **Frontend**: One small React panel that shows status/logs and triggers training.  
- **Training script**: Accept a manifest, fetch from CDN, project schema, and hash repo for attribution.  
- **Studio reuse**: List Teamspace studios; reuse running, restart stopped, create only if needed (respect free-tier fallback to L40S).

---

## Implementation plan (≤2h)

| Step | Task | Time |
|------|------|------|
| 1 | Add React training panel (`AirshipFrontendTrainingPanel`) | 30m |
| 2 | Create manifest generator (`scripts/build-training-manifest.js`) | 20m |
| 3 | Patch training script (`train.py`) for CDN + schema projection + repo hashing | 30m |
| 4 | Add Studio reuse guard (`scripts/lightning-studio-reuse.js`) | 20m |
| 5 | Wire frontend → manifest → script (backend route optional) | 20m |

---

## Code (final, consolidated)

### 1) Frontend training panel (React)

```tsx
// src/components/AirshipFrontendTrainingPanel.tsx
import React, { useState } from "react";

export function AirshipFrontendTrainingPanel() {
  const [status, setStatus] = useState("idle");
  const [logs, setLogs] = useState<string[]>([]);

  const startTraining = async () => {
    setStatus("building_manifest");
    setLogs([]);

    try {
      // If you expose a backend route:
      const res = await fetch("/api/surrogate/training/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: "latest" })
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStatus("running");
      setLogs((l) => [...l, `Manifest: ${data.manifest_path}`]);

      // SSE for logs (optional)
      const es = new EventSource(`/api/surrogate/training/logs?run_id=${data.run_id}`);
      es.onmessage = (e) => setLogs((l) => [...l, e.data]);
      es.onerror = () => {
        es.close();
        setStatus("completed");
      };
    } catch (err) {
      // Fallback: run local script via streaming (dev)
      setStatus("local_fallback");
      setLogs((l) => [...l, `Backend unavailable, run manifest + script locally`]);
      setLogs((l) => [...l, `node scripts/build-training-manifest.js --date latest`]);
      setLogs((l) => [...l, `python train.py --manifest ./manifests/training-manifest-latest.json`]);
      setStatus("error");
    }
  };

  return (
    <div style={{ padding: 16, border: "1px solid #ccc", borderRadius: 8 }}>
      <h3>Surrogate-1 Training (HF CDN mode)</h3>
      <button onClick={startTraining} disabled={status === "running"}>
        Build + Train (HF CDN)
      </button>
      <div style={{ marginTop: 12, fontSize: 13 }}>
        <strong>Status:</strong> {status}
      </div>
      <pre style={{ maxHeight: 240, overflow: "auto", background: "#f6f6f6", padding: 8 }}>
        {logs.join("\n")}
      </pre>
    </div>
  );
}
```

### 2) Build training manifest (Node)

```js
// scripts/build-training-manifest.js
#!/usr/bin/env node
// Usage: node build-training-manifest.js --repo "org/ds-mirror" --date "2026-04-29" --out "./manifests"
const { HfApi } = require("@huggingface/hub");
const fs = require("fs");
const path = require("path");

async function buildManifest({ repo, date, outDir }) {
  const api = new HfApi();
  const folderPath = `batches/mirror-merged/${date}`;
  console.log(`Listing ${repo}/${folderPath} (non-recursive)...`);
  const tree = await api.listRepoTree({ repo, path: folderPath, recursive: false });

  const files = tree
    .filter((t) => t.type === "file" && t.path.endsWith(".parquet"))
    .map((t) => t.path);

  const manifest = {
    date,
    repo,
    cdn_root: `https://huggingface.co/datasets/${repo}/resolve/main/${folderPath}/`,
    files: files.map((f) => path.relative(folderPath, f))
  };

  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, `training-manifest-${date}.json`);
  fs.writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written: ${outPath}`);
  return outPath;
}

// CLI
const args = require("minimist")(process.argv.slice(2));
if (!args.repo || !args.date) {
  console.error("Usage: node build-training-manifest.js --repo <repo> --date <date> [--out <dir>]");
  process.exit(1);
}

buildManifest({ repo: args.repo, date: args.date, outDir: args.out || "./manifests" })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
```

### 3) Lightning Studio reuse guard (JS helper)

```js
// scripts/lightning-studio-reuse.js
#!/usr/bin/env node
// Reuse or start a Lightning Studio for surrogate-1 training
const { Lightning, Teamspace, Machine } = require("lightning");

async function getOrCreateStudio({ name = "surrogate-1-training", machine = Machine.L40S } = {}) {
  const teamspace = new Teamspace();
  const studios = await teamspace.studios();

  const existing = studios.find((s) => s.name === name);
  if (existing) {
    if (existing.status === "running") {
      console.log(`Reusing running studio: ${name}`);
      return existing;
    }
    console.log(`Starting stopped studio: ${name}`);
    await existing.start({ machine });
    return existing;
  }

  console.log(`Creating studio: ${name}`);
  return Lightning.Studio({ name, create_ok: true, machine });
}

module.exports = { getOrCreateStudio };
```

### 4) Training script (CDN-only, schema projection, repo hashing)

```python
# train.py  (partial)
import json
import hashlib
import requests
import pyarrow.parquet as pq
from typing import List, Dict

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def repo_hash(repo: str) -> str:
    return hashlib.sha256(repo.encode()).hexdigest()[:16]

def fetch_parquet_via_cdn(cdn_root: str, filename: str, timeout: int = 30) -> bytes:
    url = f"{cdn_root}{filename}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_prompt_response(table) -> List[Dict]:
    # Explicit schema projection to avoid pyarrow.CastError
    rows = []
    cols = set
