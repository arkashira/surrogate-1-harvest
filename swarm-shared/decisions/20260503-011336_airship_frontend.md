# airship / frontend

## Final Consolidated Plan  
**Title:** Airship Frontend — HF CDN Bypass + Training Manifest + Studio Reuse  
**Scope:** Frontend-only (React + Lightning SDK + small Node helpers). No infra/backend changes.  
**Goal:** Eliminate HF API 429s and `pyarrow.CastError`, reduce Lightning Studio quota burn via reuse, and enable resilient frontend-driven surrogate training launch.  

---

### 1) Unified Architecture (resolve contradictions)
- **One manifest generator** (not two separate services) that:
  - Lists a single date-folder via HF API `list_repo_tree(..., recursive=False)` (rate-limit friendly).
  - Emits `file-list.json` and then `training-manifest.json` in one flow.
- **CDN-only fetches** in training:
  - Use `resolve/main/...` URLs with no Authorization header.
  - Avoid `load_dataset(streaming=True)` and mixed-schema pitfalls that cause `pyarrow.CastError`.
- **Lightning Studio reuse policy** (single source of truth):
  - Reuse a running studio by deterministic name.
  - If stopped, restart on `L40S` (preferred) with fallback to public tier.
  - Never start duplicate studios for the same date-folder.
- **Frontend orchestration**:
  - Single React launcher component.
  - Mac-side helpers invoked via API routes (or direct Node execution) to list files and produce manifest before `.run()`.

---

### 2) Implementation Steps (≤2h)

1. **Add frontend training launcher** (`AirshipTrainLauncher.tsx`)
   - Inputs: repo, date folder, HF token (masked), preferred cloud/size.
   - Actions:
     - Call `/api/hf/list-and-manifest` (or invoke local Node script) to produce `training-manifest.json`.
     - Reuse or start Lightning Studio (`surrogate-{dateFolder}`).
     - Launch training via Lightning SDK `target.run()` with manifest path/env.
   - Guards:
     - Prevent duplicate studios.
     - Restart stopped studios before run.
     - Idle timeout handling.

2. **Add unified manifest generator script** (`scripts/make-training-manifest.js`)
   - Combines listing + manifest creation.
   - Uses HF API `list_repo_tree` for the date folder.
   - Outputs `training-manifest.json` with:
     - CDN-only URLs.
     - Projection to `{prompt, response}` at parse time.
     - Attribution pattern: `batches/mirror-merged/{date}/{slug}.parquet`.
   - Rate-limit handling: single call per folder; retry after 360s on 429.

3. **Update training script stub** (`training/train.py`)
   - Read `training-manifest.json`.
   - Download via CDN URLs (requests, no HF auth).
   - Parse each file individually; project fields at parse time.
   - No `source`/`ts` columns; attribution via filename pattern.

4. **Add Lightning Studio reuse helper** (`scripts/lightning-studio-ctl.js`)
   - List `Teamspace.studios`.
   - Reuse running studio by name.
   - Restart stopped studio on `L40S`.

5. **Wire into Arkship UI**
   - Add “Train Surrogate” card.
   - Show studio status, last run, file count.
   - Stream logs via Lightning run logs.

---

### 3) Code Snippets (merged best parts)

#### Frontend Training Launcher (React)

```tsx
// packages/airship-frontend/src/components/AirshipTrainLauncher.tsx
import React, { useState } from "react";
import { Lightning } from "@lightningai/sdk";

interface Props {
  repo: string;
  dateFolder: string;
  hfToken: string;
}

export const AirshipTrainLauncher: React.FC<Props> = ({ repo, dateFolder, hfToken }) => {
  const [status, setStatus] = useState("idle");
  const [studio, setStudio] = useState<any>(null);

  const generateManifest = async () => {
    const res = await fetch("/api/hf/list-and-manifest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, dateFolder, hfToken }),
    });
    if (!res.ok) throw new Error("Manifest generation failed");
    return res.json(); // { manifestPath, fileCount }
  };

  const reuseOrStartStudio = async (name: string) => {
    const teamspace = await Lightning.Teamspace.current();
    const studios = await teamspace.studios();
    const running = studios.find((s: any) => s.name === name && s.status === "Running");
    if (running) return running;

    const stopped = studios.find((s: any) => s.name === name && s.status === "Stopped");
    if (stopped) {
      await stopped.target.start({ machine: Lightning.Machine.L40S });
      return stopped;
    }

    return await teamspace.studios.create({
      name,
      machine: Lightning.Machine.L40S,
      cloud: "lightning-lambda-prod",
    });
  };

  const launch = async () => {
    setStatus("generating");
    const { manifestPath, fileCount } = await generateManifest();
    setStatus("studio");
    const st = await reuseOrStartStudio(`surrogate-${dateFolder}`);
    setStudio(st);

    setStatus("run");
    const run = await st.run({
      command: `bash train.sh ${repo} ${dateFolder} ${manifestPath}`,
      env: {
        HF_TOKEN: hfToken,
        TRAINING_MANIFEST: manifestPath,
      },
    });

    setStatus("running");
    console.log("Run started:", run.id);
  };

  return (
    <div>
      <button onClick={launch} disabled={status !== "idle"}>
        Launch Training ({dateFolder})
      </button>
      <pre>Status: {status}</pre>
      {studio && <pre>Studio: {studio.name} ({studio.status})</pre>}
    </div>
  );
};
```

#### Unified Manifest Generator (Node)

```js
// scripts/make-training-manifest.js
#!/usr/bin/env node
const { HfApi } = require("@huggingface/hub");
const fs = require("fs");

async function makeManifest(repo, dateFolder, outPath) {
  const api = new HfApi({ token: process.env.HF_TOKEN });
  const prefix = `${dateFolder}/`;
  let files = [];

  try {
    const tree = await api.listRepoTree({ repo, path: prefix, recursive: false });
    files = tree
      .filter((f) => f.type === "file")
      .map((f) => ({
        path: f.path,
        cdn: `https://huggingface.co/datasets/${repo}/resolve/main/${f.path}`,
      }));
  } catch (err) {
    if (err.status === 429) {
      console.error("Rate limited (429). Wait 360s and retry.");
      process.exit(2);
    }
    throw err;
  }

  const manifest = {
    files: files.map((f) => ({
      cdn: f.cdn,
      projection: ["prompt", "response"],
      attribution: {
        pattern: "batches/mirror-merged/{date}/{slug}.parquet",
        file: f.path,
      },
    })),
    use_hf_api: false,
  };

  fs.writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written to ${outPath} with ${files.length} files`);
  return { manifestPath: outPath, fileCount: files.length };
}

if (require.main === module) {
  const [repo, dateFolder, out] = process.argv.slice(2);
  makeManifest(repo, dateFolder, out || "training-manifest.json").catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

#### Training Script Stub (Python)

```python
# training/train.py
import json
import requests
import pyarrow.parquet as pq
from io import BytesIO

def load_from_manifest(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    for entry in manifest["files
