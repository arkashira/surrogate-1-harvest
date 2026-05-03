# airship / frontend

## Highest-Value Incremental Improvement
Add an **HF CDN-bypass dataset loader with Lightning Studio reuse** to the frontend training UI.  
- Eliminates HF API 429s during dataset enumeration and training by using CDN-only fetches.  
- Cuts Lightning quota waste by reusing running Studios instead of recreating.  
- Fits in <2h and is immediately testable via the existing training UI.

---

## Implementation Plan (≤2h)

1. **Expose file-list JSON from backend** (15m)  
   Add endpoint `/api/training/file-list` that calls `list_repo_tree(path, recursive=False)` once (after rate-limit window) and returns `{ "date": "YYYY-MM-DD", "files": [...] }`. Cache for 6h.

2. **Frontend: add CDN-bypass loader component** (45m)  
   - New `CdnDatasetLoader.vue` (or `.tsx`) that:
     - Accepts `repo`, `dateFolder`, `fileList` (from step 1).
     - Builds CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{dateFolder}/{file}`.
     - Streams/fetches in chunks and projects to `{ prompt, response }` client-side (or passes list to Lightning training script).
     - Emits `fileCount`, `estimatedSize`, and a `startTraining` event.

3. **Lightning Studio reuse hook** (30m)  
   - Add `useLightningStudio.ts` composable:
     - On mount, call `Teamspace.studios`, find running studio by name.
     - If running: attach and reuse.
     - If stopped: restart with `target.start(Machine.L40S)`.
     - Expose `run(script)` that checks status before each call.

4. **Training script adapter** (20m)  
   - Update `train.py` to accept `--file-list-json` and use CDN-only URLs (no `load_dataset` with auth).  
   - Remove any `hf_hub_download`/`list_repo_files` recursive calls.

5. **UI integration + tests** (10m)  
   - Wire into existing training page.  
   - Add simple smoke test: load file list, validate CDN fetch for first file, ensure studio reuse prevents duplicate creation.

---

## Code Snippets

### Backend: FastAPI file-list endpoint
```python
# arkship/api/training.py
from fastapi import APIRouter, HTTPException
from huggingface_hub import HfApi
from datetime import datetime, timedelta
from functools import lru_cache

router = APIRouter()
api = HfApi()

@lru_cache(maxsize=4)
def _cached_file_list(repo: str, date_folder: str):
    # Single API call; CDN downloads are not counted against auth rate limits.
    try:
        tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
        return [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF tree error: {exc}")

@router.get("/file-list")
def get_file_list(repo: str, date: str):
    # date format YYYY-MM-DD expected
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    files = _cached_file_list(repo, date)
    return {"repo": repo, "date": date, "files": files, "count": len(files)}
```

### Frontend: CDN dataset loader (Vue 3 + TypeScript)
```vue
<!-- components/CdnDatasetLoader.vue -->
<script setup lang="ts">
import { ref, watch } from "vue";

const props = defineProps<{
  repo: string;
  date: string;
  files: string[];
}>();

const emit = defineEmits<{
  (e: "loaded", payload: { count: number; size: number }): void;
  (e: "train", payload: { fileUrls: string[] }): void;
}>();

const loading = ref(false);
const selected = ref<string[]>([]);

const cdnBase = `https://huggingface.co/datasets/${props.repo}/resolve/main/${props.date}`;

async function validateFirstFile() {
  if (!props.files.length) return;
  loading.value = true;
  try {
    const url = `${cdnBase}/${props.files[0]}`;
    const res = await fetch(url, { method: "HEAD" });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    emit("loaded", { count: props.files.length, size: Number(res.headers.get("content-length") || 0) });
  } catch (err) {
    console.error(err);
    // fallback: still allow training attempt; Lightning will report failure
  } finally {
    loading.value = false;
  }
}

function startTraining() {
  const fileUrls = selected.value.length
    ? selected.value.map((f) => `${cdnBase}/${f}`)
    : props.files.map((f) => `${cdnBase}/${f}`);
  emit("train", { fileUrls });
}

watch(() => props.files, validateFirstFile, { immediate: true });
</script>

<template>
  <div class="cdn-loader">
    <p>Files: {{ files.length }}</p>
    <ul v-if="files.length < 20">
      <li v-for="f in files" :key="f">
        <label>
          <input type="checkbox" :value="f" v-model="selected" />
          {{ f }}
        </label>
      </li>
    </ul>
    <p v-else>Showing all {{ files.length }} files (select subset if desired).</p>
    <button @click="startTraining" :disabled="loading">Start Training (CDN)</button>
  </div>
</template>
```

### Lightning Studio reuse composable
```ts
// composables/useLightningStudio.ts
import { Lightning, L40S, Teamspace } from "@lightningai/sdk";
import { ref, onMounted } from "vue";

export function useLightningStudio(studioName: string) {
  const studio = ref<any>(null);
  const status = ref<"idle" | "running" | "starting">("idle");

  async function ensureRunning() {
    const studios = await Teamspace.studios();
    let found = studios.find((s: any) => s.name === studioName && s.status === "Running");
    if (found) {
      studio.value = found;
      status.value = "running";
      return found;
    }

    // reuse stopped studio or create new
    found = studios.find((s: any) => s.name === studioName);
    if (found) {
      status.value = "starting";
      await found.start({ machine: L40S });
      studio.value = found;
    } else {
      studio.value = await Lightning.Studio.create({
        name: studioName,
        machine: L40S,
        create_ok: true,
      });
    }
    status.value = "running";
    return studio.value;
  }

  async function run(script: string, args?: Record<string, any>) {
    const s = await ensureRunning();
    return s.run(script, args);
  }

  onMounted(() => {
    ensureRunning().catch(console.error);
  });

  return { studio, status, run, ensureRunning };
}
```

### Training script: accept CDN file list
```python
# surrogate/train.py
import argparse
import json
import requests
from torch.utils.data import IterableDataset, DataLoader

class CdnParquetDataset(IterableDataset):
    def __init__(self, file_urls):
        self.file_urls = file_urls

    def __iter__(self):
        for url in self.file_urls:
            # stream parquet from CDN; project to {prompt,response}
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            # simplified: use pyarrow.parquet or fastparquet on bytes
            # project only prompt/response fields; drop others
            # yield {"prompt": ..., "response": ...}
            # (implementation depends on exact schema)
            pass

def main():
    parser = argparse.ArgumentParser
