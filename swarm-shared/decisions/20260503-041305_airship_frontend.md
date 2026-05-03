# airship / frontend

# airship / frontend — Highest-value incremental improvement (<2h)

## Chosen improvement
**Embed a pre-listed CDN file manifest into the Surrogate training frontend/config so Lightning Studio jobs fetch data via CDN only (zero HF API calls during training) and auto-recover from idle timeouts.**  

This directly eliminates HF 429s during data loading and prevents idle-stop training deaths while staying frontend-focused (config/manifest + launcher script). No schema changes, no infra changes — uses existing paths and Lightning SDK.

---

## Implementation plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1. Create file-listing artifact (one-time on Mac) | me | 10m | `list_repo_tree(recursive=False)` for today’s `batches/mirror-merged/{date}/` → `training-files.json` |
| 2. Add manifest to frontend config | me | 10m | Place `training-files.json` in `airship/frontend/config/surrogate-training/` and expose via `getTrainingFileList()` |
| 3. Update training launcher (Lightning) | me | 30m | Modify `airship/frontend/scripts/launch-surrogate-training.js` to: <br>• accept file list <br>• embed list into `train.py` template (or pass as JSON) <br>• wrap `studio.run()` with status check + auto-restart on stopped |
| 4. Update train.py template (CDN-only loader) | me | 30m | Replace `load_dataset(...)` with CDN fetch using `https://huggingface.co/datasets/{repo}/resolve/main/{path}` and project `{prompt,response}` only |
| 5. Add idle-resilience loop | me | 20m | Before each `.run()`, check `studio.status`; if not `running`, call `studio.start(machine=Machine.L40S)` and wait ready |
| 6. Smoke test (local dry-run) | me | 20m | Run launcher in dry-run mode (no GPU) to verify manifest injection and CDN URL construction |
| Buffer | — | 10m | — |

Total: ~2h

---

## Code snippets

### 1) File listing (run once on Mac) — `scripts/list-training-files.js`
```js
// scripts/list-training-files.js
// Run on Mac (or any dev machine) after HF API window clears.
// Output: frontend/config/surrogate-training/training-files.json
import { HfApi } from "@huggingface/hub";
import fs from "fs";
import path from "path";

const api = new HfApi();
const repo = "your-org/surrogate-dataset"; // <- set actual repo
const dateFolder = new Date().toISOString().slice(0, 10); // e.g., 2026-05-03
const outDir = path.resolve("frontend/config/surrogate-training");
const outFile = path.join(outDir, "training-files.json");

async function listFiles() {
  try {
    // Non-recursive per folder to avoid pagination explosion
    const tree = await api.listRepoTree({
      repo,
      path: `batches/mirror-merged/${dateFolder}`,
      recursive: false,
    });

    const files = (tree.files || [])
      .filter((f) => f.path.endsWith(".parquet"))
      .map((f) => ({
        path: f.path,
        // CDN URL (no auth, bypasses API rate limits)
        cdn_url: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(f.path)}`,
        size: f.size,
      }));

    if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(outFile, JSON.stringify({ date: dateFolder, repo, files }, null, 2));
    console.log(`Wrote ${files.length} files to ${outFile}`);
  } catch (err) {
    console.error("Failed to list files:", err);
    process.exit(1);
  }
}

listFiles();
```

### 2) Frontend config accessor — `frontend/config/surrogate-training/index.js`
```js
// frontend/config/surrogate-training/index.js
import fs from "fs";
import path from "path";

const MANIFEST_PATH = path.resolve("frontend/config/surrogate-training/training-files.json");

export function getTrainingFileList() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    throw new Error("Training file manifest not found. Run scripts/list-training-files.js first.");
  }
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf-8"));
}
```

### 3) Launcher with idle resilience — `frontend/scripts/launch-surrogate-training.js`
```js
// frontend/scripts/launch-surrogate-training.js
import { Lightning, L40S, Machine } from "@lightning-ai/sdk"; // adjust import per actual SDK
import { getTrainingFileList } from "../config/surrogate-training/index.js";
import { execSync } from "child_process";
import fs from "fs";
import path from "path";

const MANIFEST = getTrainingFileList();
const STUDY_NAME = "surrogate-training";
const REPO_ROOT = path.resolve(".."); // adjust as needed

async function ensureStudioRunning(studio) {
  const status = studio.status;
  if (status === "running") return true;
  console.log(`Studio ${studio.name} is ${status}. Starting L40S...`);
  await studio.start({ machine: Machine.L40S });
  // wait ready
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    await studio.refresh();
    if (studio.status === "running") return true;
  }
  throw new Error("Studio failed to start within timeout");
}

function injectFileListIntoTrainPy(fileList) {
  const templatePath = path.join(REPO_ROOT, "surrogate", "train_template.py");
  const outPath = path.join(REPO_ROOT, "surrogate", "train.py");
  let tpl = fs.readFileSync(templatePath, "utf-8");
  // Inject JSON as a module-level constant
  const injected = `TRAINING_FILES = ${JSON.stringify(fileList.files, null, 2)}\n` + tpl;
  fs.writeFileSync(outPath, injected);
  return outPath;
}

async function main() {
  const studio = Lightning.Studio({ name: STUDY_NAME, create_ok: true });
  await ensureStudioRunning(studio);

  // Prepare train.py with CDN file list
  const trainPy = injectFileListIntoTrainPy(MANIFEST);
  console.log("Injected file list into train.py");

  // Run training (non-blocking; monitor separately if needed)
  const run = await studio.run({
    entrypoint: ["python", "train.py"],
    source_dir: path.join(REPO_ROOT, "surrogate"),
  });

  console.log("Training run submitted:", run.id);
  return run;
}

if (require.main === module) {
  main().catch((err) => {
    console.error("Launcher failed:", err);
    process.exit(1);
  });
}
```

### 4) CDN-only loader snippet for `train_template.py`
```python
# surrogate/train_template.py
# TRAINING_FILES injected by launcher (list of dicts with 'cdn_url' and 'path')
import pyarrow.parquet as pq
import requests
import io
import os

def cdn_fetch_parquet(cdn_url):
    # CDN fetch — no Authorization header, bypasses HF API rate limits
    resp = requests.get(cdn_url, timeout=60)
    resp.raise_for_status()
    return pq.read_table(io.BytesIO(resp.content))

def build_dataset():
    rows = []
    for item in TRAINING_FILES:
        tbl = cdn_fetch_parquet(item["cdn_url"])
        # Project only {prompt, response}; ignore other columns
        if "prompt" in tbl.column_names and "response" in tbl.column_names:
            for i in range(tbl.num_rows):
                rows.append({
                    "prompt": tbl["prompt"][i].as_py(),
                    "response": tbl["response"][i].as_py(),
                })
    return rows
```

---

## Acceptance criteria
- [ ] `scripts/list-training-files.js` produces valid `training-files.json` with CDN URLs.
- [ ] `
