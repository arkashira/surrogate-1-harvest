# vanguard / frontend

## 1. Diagnosis

- Frontend triggers HF dataset enumeration at runtime (`list_repo_tree`/`load_dataset`) instead of using a pre-computed CDN manifest → exposes 429 rate-limit risk and non-reproducible runs.
- No deterministic, content-addressed file list keyed by date/slug available to the UI → jobs re-enumerate on every load and can diverge across environments.
- Missing lightweight manifest artifact (JSON) that can be embedded/bundled into the frontend build so data selection is offline-first and CDN-only during runtime.
- No clear UX for selecting date/slug for training runs; users rely on live API calls that can fail or return inconsistent results.
- Frontend bundle has no fallback when HF API is unavailable; cannot list available training folders without network + auth.

## 2. Proposed change

Add a frontend-first CDN manifest workflow:

- Create `/opt/axentx/vanguard/frontend/src/lib/data/manifest.ts` — types + loader for a static `manifest.json`.
- Create `/opt/axentx/vanguard/frontend/src/lib/data/manifest.json` (generated) — contains `{ date, slug, files: { prompt, response } }` entries produced from a one-time Mac-side HF enumeration (respecting rate limits) and committed to repo.
- Update the training run selector UI (`/opt/axentx/vanguard/frontend/src/components/TrainingSelector.svelte` or equivalent) to consume the local manifest instead of calling HF APIs at runtime.
- Add a small CLI helper (`/opt/axentx/vanguard/scripts/gen-manifest.sh`) to regenerate the manifest safely (single `list_repo_tree` call per date folder) and project only `{prompt,response}` paths; outputs deterministic JSON keyed by `{date}/{slug}`.

Scope: frontend source only (no backend changes). Goal: eliminate runtime HF API calls for file listing and make runs reproducible.

## 3. Implementation

```bash
# Ensure scripts dir exists
mkdir -p /opt/axentx/vanguard/scripts
mkdir -p /opt/axentx/vanguard/frontend/src/lib/data
```

### 3.1 CLI helper to generate manifest (run on Mac, respects HF rate limits)

`/opt/axentx/vanguard/scripts/gen-manifest.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: gen-manifest.sh <dataset_repo> <date_folder> <out_json>
# Example: gen-manifest.sh axentx/surrogate-1 2026-04-29 ./frontend/src/lib/data/manifest.json

REPO="${1:-axentx/surrogate-1}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-./frontend/src/lib/data/manifest.json}"

# Use huggingface_hub from Python (safe, single API call)
python3 - "$REPO" "$DATE_FOLDER" "$OUT" <<'PY'
import json
import os
import sys
from huggingface_hub import list_repo_tree

REPO = sys.argv[1]
DATE_FOLDER = sys.argv[2]
OUT = sys.argv[3]

# Single non-recursive call per date folder (avoids pagination explosion)
tree = list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)

entries = []
for item in tree:
    # Expecting folder-per-slug under date: 2026-04-29/<slug>/prompt.txt,response.txt
    if not item.rfilename.endswith("/"):
        continue
    slug = item.rfilename.rstrip("/")
    slug_path = f"{DATE_FOLDER}/{slug}"

    # List files inside slug folder (one more non-recursive call; small)
    try:
        files_tree = list_repo_tree(repo_id=REPO, path=slug_path, recursive=False)
    except Exception:
        continue

    file_names = [f.rfilename for f in files_tree if not f.rfilename.endswith("/")]
    prompt = next((f for f in file_names if "prompt" in f.lower()), None)
    response = next((f for f in file_names if "response" in f.lower()), None)

    if prompt and response:
        entries.append({
            "date": DATE_FOLDER,
            "slug": slug,
            "path": slug_path,
            "files": {
                "prompt": prompt,
                "response": response,
                # CDN URLs (no auth, bypasses /api/ rate limits)
                "prompt_cdn": f"https://huggingface.co/datasets/{REPO}/resolve/main/{prompt}",
                "response_cdn": f"https://huggingface.co/datasets/{REPO}/resolve/main/{response}",
            }
        })

os.makedirs(os.path.dirname(OUT) if os.path.dirname(OUT) else ".", exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(entries, f, indent=2, sort_keys=True)

print(f"Wrote {len(entries)} entries to {OUT}")
PY
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/gen-manifest.sh
```

Run once to generate initial manifest (do this now):
```bash
cd /opt/axentx/vanguard
./scripts/gen-manifest.sh axentx/surrogate-1 2026-04-29 ./frontend/src/lib/data/manifest.json
```

### 3.2 Frontend manifest loader and types

`/opt/axentx/vanguard/frontend/src/lib/data/manifest.ts`
```ts
export interface ManifestFile {
  prompt: string;
  response: string;
  prompt_cdn: string;
  response_cdn: string;
}

export interface ManifestEntry {
  date: string;
  slug: string;
  path: string;
  files: ManifestFile;
}

let _manifest: ManifestEntry[] | null = null;

export async function loadManifest(): Promise<ManifestEntry[]> {
  if (_manifest) return _manifest;
  const res = await fetch('/src/lib/data/manifest.json', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to load manifest.json');
  _manifest = await res.json();
  return _manifest;
}

export async function getEntry(date: string, slug: string): Promise<ManifestEntry | undefined> {
  const m = await loadManifest();
  return m.find((e) => e.date === date && e.slug === slug);
}
```

### 3.3 Update training selector UI to use local manifest

If a Svelte component exists (common in this repo), update it; otherwise create a minimal selector.

`/opt/axentx/vanguard/frontend/src/components/TrainingSelector.svelte`
```svelte
<script lang="ts">
  import { loadManifest, type ManifestEntry } from '$lib/data/manifest';

  let entries: ManifestEntry[] = [];
  let selected: ManifestEntry | null = null;
  let loading = true;
  let error: string | null = null;

  (async () => {
    try {
      entries = await loadManifest();
      if (entries.length > 0) selected = entries[0];
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  })();

  function select(entry: ManifestEntry) {
    selected = entry;
  }
</script>

<div class="training-selector">
  {#if loading}
    <p>Loading available runs...</p>
  {:else if error}
    <p class="error">Error: {error}</p>
  {:else}
    <label>
      Select training run (local manifest — CDN-only file access):
      <select on:change={(e) => {
        const slug = (e.target as HTMLSelectElement).value;
        const entry = entries.find((x) => x.slug === slug);
        if (entry) select(entry);
      }}>
        {#each entries as entry}
          <option value={entry.slug} selected={selected?.slug === entry.slug}>
            {entry.date} / {entry.slug}
          </option>
        {/each}
      </select>
    </label>

    {#if selected}
      <section class="selected">
        <h4>{selected.date} / {selected.slug}</h4>
        <p>Prompt: <a href={selected.files.prompt_cdn} target="_blank" rel="noreferrer">{selected.files.prompt}</a></p>
       
