# airship / frontend

**Final Consolidated Plan (Best of Both Candidates)**

**Goal (unchanged):** eliminate HF API 429s during Surrogate training and make Lightning training resilient to idle timeouts.  
**ETA:** <2h (no schema/infra changes; uses existing CDN paths and Lightning SDK).

---

## 1) CDN-only parquet loader (single-file change)

**File:** `surrogate/train.py`

- Accept a local `file_list.json` (generated once per day on the orchestrator/Mac).  
- Replace `load_dataset(..., streaming=True)` with direct CDN reads via `datasets`/`pyarrow` + `fsspec` using public URLs:  
  `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header).  
- Fallback to HF token only if CDN 404 (should not happen for mirrored files).  
- Keep streaming/iterable dataset interface to avoid full downloads and OOM.

**Minimal diff sketch:**
```python
# surrogate/train.py
import json, os
from datasets import load_dataset, IterableDataset
import fsspec

def build_cdn_loader(file_list_path: str, repo: str = "axentx/surrogate-mirror"):
    with open(file_list_path) as f:
        files = [f["path"] for f in json.load(f)["files"]]

    def gen():
        base = f"https://huggingface.co/datasets/{repo}/resolve/main"
        for p in files:
            url = f"{base}/{p}"
            # stream parquet row-groups via fsspec + pyarrow
            with fsspec.open(url, "rb") as fp:
                # lightweight streaming: yield batches/rows as needed
                yield from read_parquet_stream(fp)
    return IterableDataset.from_generator(lambda: gen())
```

**Acceptance criteria:** training runs with `HF_TOKEN` unset; no 429s from dataset reads.

---

## 2) Orchestrator: generate CDN file-list once per day

**Script:** `scripts/generate-cdn-file-list.js` (kept from Candidate 1; small fixes)

- Runs once per day (cron) or at container start if missing.  
- Requires `HF_TOKEN` only at generation time (not during training).  
- Writes `public/cdn-file-list.json` into Arkship build artifact **and** copies to `surrogate/file_list.json` for trainer.  
- Uses `listRepoTree` non-recursive per date folder; filters `.parquet`.

**Robustness fix:** validate JSON schema and ensure non-empty list; exit non-zero on failure so cron alerts.

```js
// scripts/generate-cdn-file-list.js
import { HfApi } from "@huggingface/hub";
import fs from "fs";
import path from "path";

const OUT_WEB = path.resolve("./public/cdn-file-list.json");
const OUT_TRAINER = path.resolve("./surrogate/file_list.json");
const REPO = "axentx/surrogate-mirror";
const FOLDER = new Date().toISOString().slice(0, 10);

async function main() {
  const api = new HfApi({ token: process.env.HF_TOKEN });
  const tree = await api.listRepoTree({ repo: REPO, path: FOLDER, recursive: false });
  const files = (tree.files || [])
    .filter((f) => f.path.endsWith(".parquet"))
    .map((f) => ({
      path: f.path,
      cdn: `https://huggingface.co/datasets/${REPO}/resolve/main/${f.path}`,
      size: f.size,
    }));
  if (files.length === 0) throw new Error("No parquet files found");
  const payload = { date: FOLDER, files };
  fs.mkdirSync(path.dirname(OUT_WEB), { recursive: true });
  fs.writeFileSync(OUT_WEB, JSON.stringify(payload, null, 2));
  fs.writeFileSync(OUT_TRAINER, JSON.stringify(payload, null, 2));
  console.log(`Wrote ${files.length} files`);
}
main().catch((err) => { console.error(err); process.exit(1); });
```

Add to entrypoint:
```bash
node /app/scripts/generate-cdn-file-list.js || true
```

---

## 3) Lightning runner: idle-resilient auto-restart

**Mechanism:** lightweight orchestration script + Arkship API endpoints.

- Orchestrator script (`scripts/watchdog-lightning.js`) polls Lightning studio state every 30s.  
- If studio is `stopped`/`failed`, it calls `studio.start({ machine: "L40S" })` and re-runs with `--file-list surrogate/file_list.json`.  
- Exponential backoff on repeated failures; alert on persistent errors.

**Arkship API endpoints** (from Candidate 1, tightened):

`GET /api/v1/training/status`
- Returns: `{ studioRunning, studioName, lastFileCount, lastRefresh, fileListUrl }`  
- Derives `lastFileCount` from local `surrogate/file_list.json` (fast, no CDN fetch in status path).

`POST /api/v1/training/resume`
- Ensures studio exists; starts if stopped; runs trainer with CDN file-list.  
- Idempotent: safe for watchdog and manual use.

**Concrete trainer command used by resume/watchdog:**
```
python surrogate/train.py --file-list surrogate/file_list.json --machine L40S
```

---

## 4) Frontend: Training status panel (non-blocking)

**File:** `/arkship/src/components/TrainingStatus.tsx` (kept from Candidate 1; minor fixes)

- Polls `/api/v1/training/status` every 10s.  
- Shows RUN/STOPPED, file count, last refresh, and Resume button (only when stopped).  
- Links to `cdn-file-list.json` for quick copy/verify.

**Polling fix:** cancel on unmount; retry on network errors without spamming.

---

## 5) Contradictions resolved

- **Loader approach:** Use CDN-only loader as primary (Candidate 2) but keep HF token fallback for robustness (Candidate 1 concern). Accept `file_list.json` produced by orchestrator (both agree).  
- **Where file-list lives:** Write to both `public/` (UI) and `surrogate/` (trainer) so neither component depends on runtime CDN fetch for status/counts.  
- **Auto-restart:** Put watchdog logic in orchestrator script + API endpoints (Candidate 1) rather than inside trainer (clean separation).  
- **Build vs runtime:** Generate file-list at container start (Candidate 1) and also via cron (Candidate 2) for daily refresh; both coexist.

---

## 6) Rollout checklist (<2h)

1. Add/modify `surrogate/train.py` to accept `--file-list` and use CDN loader with HF token fallback.  
2. Add `scripts/generate-cdn-file-list.js` and entrypoint invocation.  
3. Add `scripts/watchdog-lightning.js` and lightweight cron/supervisord entry.  
4. Add Arkship routes `/api/v1/training/status` and `/resume`.  
5. Add frontend `TrainingStatus.tsx` and include in Arkship UI.  
6. Test: run trainer with `HF_TOKEN` unset; stop studio manually; verify watchdog restarts and training resumes using CDN file-list.
