# airship / frontend

## Final Consolidated Implementation (Best Parts + Resolved Contradictions)

**Chosen approach**: CDN-first training slice that front-loads HF listing once, reuses a Lightning Studio, and projects to `{prompt, response}` at parse time to avoid mixed-schema errors and HF 429s during training.

**Key decisions (resolve contradictions)**:
- Use `dateFolder` (not `datePath`) for consistency with HF mirror layout and code examples.
- Save the file list to a deterministic path (`tmp/file-list.json`) so the training script can reliably find it (avoids glob race conditions).
- Reuse a running Lightning Studio if present; restart only if stopped (not “always recreate”). This minimizes quota waste and avoids idle-timeout churn.
- Project schema at parse time (drop extra columns) to prevent pyarrow CastError on heterogeneous repo files.
- Use `hf_hub_download` + CDN URLs (`resolve/main/...`) per file; do **not** use `load_dataset(streaming=True)` on the heterogeneous repo during training.
- Keep changes minimal and deployable within ~2h; no docker-compose changes required.

---

## Implementation Plan (Actionable)

1. **Frontend UI** (`surrogate/src/components/TrainingSlice.tsx`)
   - Inputs: `repo`, `dateFolder`
   - Actions:
     - “Generate file list” → POST `/api/training/file-list` (single HF tree call) → saves `tmp/file-list.json`
     - “Start CDN training” → POST `/api/training/start` (reuse Lightning Studio, run training with CDN-only fetches)
   - Shows status, file count, Studio link

2. **API: file-list** (`surrogate/pages/api/training/file-list.ts`)
   - POST with `{ repo, dateFolder }`
   - Call HF `list_repo_tree(repo, path=dateFolder, recursive=False)` once
   - Filter to files, save deterministic `tmp/file-list.json`, return `{ repo, dateFolder, files, fileListPath }`
   - Handle errors (400/500) and validate inputs

3. **API: start** (`surrogate/pages/api/training/start.ts`)
   - POST with `{ repo, dateFolder }`
   - Call `get_or_create_studio(name, machine)` to reuse running studio or restart stopped one
   - Invoke `run_training(...)` which launches/updates the Lightning job with `fileListPath` pointing to `tmp/file-list.json`
   - Return `{ studioUrl, status, runId }`

4. **Lightning utils** (`surrogate/training/lightning_utils.py`)
   - `get_or_create_studio(name, machine)`:
     - List Teamspace studios; if Running → reuse; if Stopped → restart with target machine; else create new.
   - `run_training(opts)`:
     - Ensure `tmp/file-list.json` exists
     - Start/update Lightning Studio job with script `train_cdn.py` and args: `--file_list tmp/file-list.json --date_folder <dateFolder>`
     - Prefer `L40S`; fallback to `H200` if available in `lightning-lambda-prod`

5. **Training script** (`surrogate/training/train_cdn.py`)
   - Args: `--file_list`, `--date_folder`
   - Load `file_list.json` → list of file paths
   - For each file:
     - Use `hf_hub_download(repo, filename, repo_type="dataset")` to get local cache path (or construct CDN URL `https://huggingface.co/datasets/<repo>/resolve/main/<path>`)
     - Read parquet (or expected format) and project via `project_to_prompt_response`
   - Projection helper (`projection.py`):
     - `project_to_prompt_response(file_path)` → return `{ prompt: str, response: str }`
     - Drop extra columns (`source`, `ts`, etc.)
   - Build `datasets.Dataset`/`DataLoader` from projected rows; ensure no HF API calls during epoch iteration
   - Start training on selected machine

6. **Schema/projection helper** (`surrogate/training/projection.py`)
   - Deterministic projection; log dropped columns for visibility
   - If parquet schema varies, coerce only `prompt`/`response` and ignore remainder

7. **Docker/compose**
   - No changes required; ensure `surrogate` service ports are exposed for local dev

8. **Test flow**
   - UI → Generate file list → verify `tmp/file-list.json` and file count
   - Start CDN training → confirm Lightning Studio reused and job running
   - Spot-check training logs: only CDN fetches, no HF API 429s, schema projection clean

---

## Code Snippets (Final)

### Frontend: TrainingSlice.tsx
```tsx
// surrogate/src/components/TrainingSlice.tsx
import { useState } from 'react';

export default function TrainingSlice() {
  const [repo, setRepo] = useState('datasets/your-repo');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [fileList, setFileList] = useState<any>(null);
  const [status, setStatus] = useState('');

  const generateFileList = async () => {
    setStatus('Generating file list...');
    const res = await fetch('/api/training/file-list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, dateFolder }),
    });
    const data = await res.json();
    setFileList(data);
    setStatus(`Found ${data.files?.length || 0} files`);
  };

  const startTraining = async () => {
    setStatus('Starting CDN training...');
    const res = await fetch('/api/training/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, dateFolder }),
    });
    const data = await res.json();
    setStatus(`Studio: ${data.studioUrl} | Status: ${data.status}`);
  };

  return (
    <div style={{ padding: 20 }}>
      <h2>Surrogate CDN Training Slice</h2>
      <label>
        Repo:
        <input value={repo} onChange={(e) => setRepo(e.target.value)} style={{ marginLeft: 8, width: 300 }} />
      </label>
      <br />
      <label>
        Date folder:
        <input value={dateFolder} onChange={(e) => setDateFolder(e.target.value)} style={{ marginLeft: 8, width: 120 }} />
      </label>
      <br />
      <button onClick={generateFileList} style={{ marginTop: 8, marginRight: 8 }}>
        Generate file list
      </button>
      <button onClick={startTraining} disabled={!fileList} style={{ marginTop: 8 }}>
        Start CDN training
      </button>
      <p>{status}</p>
      {fileList && (
        <pre style={{ maxHeight: 200, overflow: 'auto' }}>
          {JSON.stringify(fileList, null, 2)}
        </pre>
      )}
    </div>
  );
}
```

### API: file-list.ts
```ts
// surrogate/pages/api/training/file-list.ts
import type { NextApiRequest, NextApiResponse } from 'next';
import { list_repo_tree } from '@huggingface/hub';
import fs from 'fs';
import path from 'path';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const { repo, dateFolder } = req.body;
  if (!repo || !dateFolder) return res.status(400).json({ error: 'repo and dateFolder required' });

  try {
    const tree = await list_repo_tree({
      repo,
      path: dateFolder,
      recursive: false,
    });

    const files = (tree.files || [])
      .filter((f: any) => !f.path.endsWith('/'))
      .map((f: any) => f.path);

    const outDir = path.join(process.cwd(), 'tmp');
    if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
    const outPath = path.join(outDir, 'file-list.json');
    fs.write
