# airship / frontend

**Final Synthesized Implementation**  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

---

## Highest-Value Incremental Improvement
**Implement HF CDN-bypass dataset loader + Lightning Studio reuse in the training UI**  
- Eliminates HF API 429s and `pyarrow.CastError`s during dataset loading  
- Reduces Lightning quota burn by reusing running Studios  
- Ships in **<2h** as **frontend-only** change (no backend/training code changes required)

---

## Implementation Plan (Frontend-only)

### 1) Add CDN-bypass file-list loader utility
- Create `src/lib/dataset/hf-cdn-loader.ts`
- Expose:
  - `listRepoTree(repoId: string, dateFolder: string): Promise<string[]>`  
    - Calls `list_repo_tree(repoId, path=dateFolder, recursive=true)` **once** from UI (or uses provided file-list JSON)  
    - Returns CDN URLs:  
      `https://huggingface.co/datasets/{repoId}/resolve/main/{dateFolder}/{file}`
  - `saveFileListForTraining(fileList: string[]): string`  
    - Returns a data URL or path to an embedded JSON file to be passed to training CLI

### 2) Add Dataset Source panel in training form
- New section: **“Dataset Source”**
- Fields:
  - `repo_id` (text, default `axentx/surrogate-data`)
  - `date_folder` (text, default `YYYY-MM-DD`)
  - `file_list_json` (textarea or upload) — optional if CDN-bypass enabled
  - Toggle: **“Use HF CDN (bypass API)”** (default **on**)
- Behavior:
  - If CDN-bypass enabled and no file list provided: call `listRepoTree` once to generate file list and CDN URLs (cached locally)
  - Preview: first 3 CDN URLs + total size
- Validation:
  - Warn if HF API would be used when file list is empty
  - Validate JSON schema: `{ "files": ["path1", ...] }`

### 3) Add Lightning Studio reuse controls
- Section: **“Lightning Studio”**
- Fields:
  - `reuse_running` (checkbox, default **true**)
  - `studio_name` (text, default `surrogate-train-{YYYY-MM-DD}`)
  - `cloud` (enum: `lightning-public-prod`, `lightning-lambda-prod`)
  - `machine` (enum: `L40S`, `H200`)
- Behavior:
  - On mount: call `Teamspace.studios.list()` (or `/api/studios`) to find running/stopped studios
  - Status indicator: **Running / Stopped / Not Found**
- Guardrails:
  - If `H200` selected, force `cloud = lightning-lambda-prod` and show error otherwise
  - If reuse enabled and studio not found: create new with selected machine on job submit

### 4) Generate train command with CDN-only flags
- Build CLI args:
  - `--use_cdn`
  - `--file_list_path <embedded-json-path>`
  - `--project_to prompt,response`
  - `--attribution_filename batches/mirror-merged/{date}/{slug}.parquet`
- If reuse enabled:
  - Add `--reuse_studio`
  - Add `--studio_name <name>`

### 5) Wire into existing training flow
- On **“Start Training”**:
  - If reuse enabled:
    - Check running studios list
    - If found and running: attach
    - If found but stopped: restart (or prompt)
    - If not found: create new studio with selected machine
  - Submit job with generated CLI args

---

## Resolved Contradictions (in favor of correctness + actionability)
1. **File list source**  
   - *Conflict*: Candidate 1 embedded JSON in UI; Candidate 2 used `list_repo_tree` call.  
   - *Resolution*: Support **both** — allow user to paste JSON (for reproducibility) **or** auto-generate via `list_repo_tree` once (for convenience). Default to auto-generation when CDN-bypass is on and no JSON provided.

2. **Studio listing endpoint**  
   - *Conflict*: Candidate 1 used `/api/studios`; Candidate 2 used `Teamspace.studios.list()`.  
   - *Resolution*: Use `Teamspace.studios.list()` if available in frontend SDK; otherwise fallback to `/api/studios`. Keep implementation flexible.

3. **Cloud/machine validation**  
   - *Conflict*: Candidate 1 allowed H200 on public cloud with only a warning.  
   - *Resolution*: Enforce correctness — **H200 requires lambda-prod**. Disable public-cloud option when H200 selected and show inline error.

4. **Default reuse behavior**  
   - *Conflict*: Candidate 2 defaulted reuse to true but didn’t specify naming; Candidate 1 used date-based name.  
   - *Resolution*: Default `reuse_running = true` with `studio_name = surrogate-train-{YYYY-MM-DD}` to maximize quota savings and determinism.

---

## Minimal Code Snippets (Merged Best Parts)

### `src/lib/dataset/hf-cdn-loader.ts`
```ts
export async function listRepoTree(repoId: string, dateFolder: string): Promise<string[]> {
  // Replace with actual SDK call or fetch to backend proxy that calls list_repo_tree
  const res = await fetch(`/api/hf/tree?repo=${encodeURIComponent(repoId)}&path=${encodeURIComponent(dateFolder)}&recursive=true`);
  const data = await res.json();
  return data.files || [];
}

export function toCdnUrls(repoId: string, dateFolder: string, files: string[]): string[] {
  const base = `https://huggingface.co/datasets/${repoId}/resolve/main/${dateFolder}`;
  return files.map(f => `${base}/${f}`);
}

export function saveFileListForTraining(files: string[]): string {
  const blob = new Blob([JSON.stringify({ files }, null, 2)], { type: 'application/json' });
  return URL.createObjectURL(blob);
}
```

### Dataset Source Panel (React)
```tsx
import { useState, useEffect } from 'react';
import { listRepoTree, toCdnUrls } from '@/lib/dataset/hf-cdn-loader';

export function DatasetSourcePanel({ onChange }: { onChange: (cfg: any) => void }) {
  const [repoId, setRepoId] = useState('axentx/surrogate-data');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [fileListJson, setFileListJson] = useState('');
  const [useCdn, setUseCdn] = useState(true);
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);

  useEffect(() => {
    const build = async () => {
      let files: string[] = [];
      if (fileListJson) {
        try { files = JSON.parse(fileListJson).files || []; } catch {}
      } else if (useCdn) {
        files = await listRepoTree(repoId, dateFolder);
      }
      const urls = useCdn ? toCdnUrls(repoId, dateFolder, files.slice(0, 3)) : [];
      setPreviewUrls(urls);
      onChange({
        repo_id: repoId,
        date_folder: dateFolder,
        use_cdn: useCdn,
        file_list: files.length ? { files } : null,
      });
    };
    build();
  }, [repoId, dateFolder, fileListJson, useCdn, onChange]);

  return (
    <div className="p-4 border rounded space-y-3">
      <h3 className="font-semibold">Dataset Source (HF CDN-bypass)</h3>

      <label className="block text-sm">
        Repo ID
        <input className="w-full p-2 border rounded" value={repoId}
          onChange={e => setRepoId(e.target.value)} />
      </label>

      <label className="block text-sm">
        Date folder
        <input className="w-full p-2 border rounded" value={dateFolder}
          onChange={e => setDateFolder(e.target.value)} />
      </label>

      <label className="block text-sm">
        File list JSON (optional if CDN-bypass enabled)
        <textarea className="w-full
