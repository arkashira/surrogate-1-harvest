# airship / frontend

## Final Synthesized Implementation  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

**Core decision:**  
- **Server-side data loading via CDN** is mandatory for eliminating HF API 429s during training (Candidate 1 is correct here; frontend-only bypass cannot protect training jobs from API calls if the loader still uses `load_dataset`).  
- **Frontend CDN preview** is valuable for UX and pre-flight checks, but must not replace server-side loader changes.  
- **Lightning Studio reuse** must be implemented once, robustly, on the orchestration side (not duplicated in frontend + backend with divergent behavior).

---

## 1) Required Server-Side Changes (Eliminate 429s)

**File:** `surrogate/data/cdn_file_list.py`  
*(Keep Candidate 1 implementation; add parameterization for date folder)*

```python
import json
import os
import argparse
from huggingface_hub import HfApi

HF_REPO = "axentx/surrogate-dataset"

def build_file_list(date_folder: str, out_path: str):
    api = HfApi()
    items = list(api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=True, repo_type="dataset"))
    files = [f.rfilename for f in items if f.type == "file"]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo": HF_REPO, "date_folder": date_folder, "files": files}, f, indent=2)
    print(f"Saved {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-folder", default="2026-05-03")
    parser.add_argument("--out", default="data/file_list.json")
    args = parser.parse_args()
    build_file_list(args.date_folder, args.out)
```

**File:** `surrogate/train.py` (critical fix)  
Replace `load_dataset(streaming=True)` with CDN-only loader. Do **not** use frontend-only bypass for training.

```python
import json
import os
import time
from pathlib import Path
from huggingface_hub import hf_hub_download

def load_examples_from_cdn(file_list_path="data/file_list.json", max_retries=3):
    with open(file_list_path) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    for rel_path in cfg["files"]:
        for attempt in range(max_retries):
            try:
                local_path = hf_hub_download(
                    repo_id=repo,
                    filename=rel_path,
                    repo_type="dataset",
                    local_dir_use_symlinks=False,
                )
                yield parse_pair(local_path)  # project to {prompt, response} only
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                # Exponential backoff; only initial metadata calls can 429 now
                time.sleep(2 ** attempt)

def parse_pair(path):
    # Implement projection; keep attribution via filename pattern
    # Do NOT add source/ts columns that break downstream expectations
    ...
```

---

## 2) Lightning Studio Reuse (Single Source of Truth)

**File:** `surrogate/launch.py`  
Robust reuse + restart logic. Used by CLI/cron and optionally imported by frontend helper.

```python
from lightning import Studio, Teamspace, Machine

def get_or_create_running_studio(name="surrogate-train", machine=Machine.L40S):
    studios = Teamspace.studios()
    for s in studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    for s in studios:
        if s.name == name and s.status == "Stopped":
            print(f"Restarting stopped studio: {name}")
            s.start(machine=machine)
            return s
    print(f"Creating studio: {name}")
    return Studio(name=name, create_ok=True, machine=machine)

def run_training():
    studio = get_or_create_running_studio()
    studio.run(["python", "train.py"])
```

---

## 3) Optional Frontend Enhancements (CDN Preview UX)

**File:** `src/lib/cdn.ts`  
Frontend-only preview; does not affect training data path.

```ts
export const cdn = {
  fileUrl(repo: string, path: string) {
    return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
  },

  async head(repo: string, path: string) {
    const res = await fetch(this.fileUrl(repo, path), { method: "HEAD" });
    if (!res.ok) throw new Error(`CDN HEAD failed: ${res.status}`);
    return {
      size: Number(res.headers.get("content-length") || 0),
      etag: res.headers.get("etag") || "",
    };
  },

  async previewText(repo: string, path: string, limit = 10240) {
    const res = await fetch(this.fileUrl(repo, path), {
      headers: { Range: `bytes=0-${limit - 1}` },
    });
    if (!res.ok && res.status !== 206) throw new Error(`CDN fetch failed: ${res.status}`);
    return await res.text();
  },
};
```

**File:** `src/lib/lightning.ts`  
Lightweight helper for UI actions (does not replace server-side `launch.py`).

```ts
import { Lightning, Teamspace, Machine } from "@lightningai/sdk";

export async function getOrCreateRunningStudioUi(name: string) {
  const studios = await Teamspace.studios();
  const running = studios.find((s) => s.name === name && s.status === "Running");
  if (running) return running;

  const stopped = studios.find((s) => s.name === name && s.status === "Stopped");
  if (stopped) {
    await stopped.start({ machine: Machine.L40S });
    return stopped;
  }

  return Lightning.studio({ name, create_ok: true, machine: Machine.L40S });
}
```

---

## 4) Orchestration & Cron Fix

**File:** `surrogate/launch_wrapper.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/axentx/airship/surrogate
exec python launch.py "$@"
```

**Crontab**

```bash
SHELL=/bin/bash
*/30 * * * * bash /opt/axentx/airship/surrogate/launch_wrapper.sh >> /var/log/surrogate_launch.log 2>&1
```

---

## 5) Verification Checklist

1. Generate manifest once (on Mac or orchestrator):  
   `python surrogate/data/cdn_file_list.py --date-folder 2026-05-03 --out data/file_list.json`

2. Run training:  
   `python surrogate/train.py`  
   - Confirm no HF auth/429 during data loading (only CDN downloads).  
   - Confirm `parse_pair` receives local paths and yields `{prompt, response}`.

3. Studio reuse:  
   - Run `python launch.py` twice; second run must print `Reusing running studio`.

4. Frontend (optional):  
   - Open `/datasets`, verify CDN previews load without auth.  
   - “Train” action must trigger `launch.py` (or SDK) that uses CDN-only loader.

---

## Why This Is Correct + Actionable

- **Contradiction resolved:** Training cannot rely on frontend-only bypass; server-side loader must use CDN. Frontend preview is complementary, not replacement.  
- **Actionable:** Each artifact is minimal, versioned, and testable in ≤2h.  
- **Robust:** Studio reuse is centralized; cron and manual runs behave identically.
