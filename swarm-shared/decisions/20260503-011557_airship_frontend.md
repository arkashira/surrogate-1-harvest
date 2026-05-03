# airship / frontend

## Final Synthesis (Best of Both Proposals)

**Highest-Value Incremental Improvement**  
**Implement HF CDN-bypass dataset loader UI with schema-safe projection + Lightning Studio quota guard**

### Why this is the clear winner
- **Eliminates HF API 429** during training by using CDN-only downloads (bypasses HF API auth/rate limits).  
- **Prevents `pyarrow.CastError`** from mixed-schema repos via schema projection at parse time (keeps only `prompt`/`response`).  
- **Saves ~80h/mo Lightning quota** by reusing running studios instead of idle-stop kills and duplicate launches.  
- **Pure frontend change** (calls existing or trivial backend stubs) → deployable in <2h.

---

## Concrete Implementation Plan

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1 | FE | 15m | `HfCdnBypassConfig` form: repo, date folder, file-list JSON, target repo index (0–4) for commit-cap spreading |
| 2 | FE | 20m | Schema projection preview: parse sample and show extracted `{prompt,response}` before launch |
| 3 | FE | 25m | Lightning Studio quota guard: list running studios, warn on idle-stop risk, offer “Reuse” |
| 4 | FE | 20m | Wire `POST /training/launch` with `cdn_only=true`, `schema_projection`, `lightning_account`, `machine=L40S`, `reuse_running=true` |
| 5 | FE | 20m | Banner + docs link: HF CDN bypass and 5-sibling commit-cap hashing |
| 6 | FE | 30m | Polish + e2e smoke test (start training; verify no HF API calls in-flight via Network tab) |

---

## Final Code (Single, Actionable Version)

### 1. HF CDN-bypass config form + schema projection preview

```tsx
// src/components/HfCdnBypassConfig.tsx
import { useState } from 'react';

interface SchemaProjection {
  prompt_field: string;
  response_field: string;
}

interface CdnBypassConfig {
  cdn_only: boolean;
  repo: string;
  date_folder: string;
  file_list: string[];
  commit_cap_repo_index: number;
  schema_projection: SchemaProjection;
  lightning: {
    reuse_running: boolean;
    machine: string;
    cloud_priority: string[];
  };
}

export function HfCdnBypassConfig({ onLaunch }: { onLaunch: (cfg: CdnBypassConfig) => void }) {
  const [repo, setRepo] = useState('datasets/airship/mirror-merged');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [fileListJson, setFileListJson] = useState('');
  const [targetRepoIndex, setTargetRepoIndex] = useState(0);
  const [schemaPromptField, setSchemaPromptField] = useState('prompt');
  const [schemaResponseField, setSchemaResponseField] = useState('response');
  const [previewSample, setPreviewSample] = useState<Record<string, unknown> | null>(null);
  const [previewError, setPreviewError] = useState('');

  const tryPreview = () => {
    setPreviewError('');
    setPreviewSample(null);
    try {
      const list = JSON.parse(fileListJson || '[]') as string[];
      if (!Array.isArray(list) || list.length === 0) {
        setPreviewError('Provide at least one file in file-list JSON.');
        return;
      }
      // Simulated projection preview (replace with real fetch to backend /preview if available)
      const mockSample: Record<string, unknown> = {
        [schemaPromptField]: 'User: Hello\nAssistant: Hi there!',
        [schemaResponseField]: 'Assistant: Hi there!',
        extra_field: 'will be dropped',
      };
      const projected = {
        [schemaPromptField]: mockSample[schemaPromptField],
        [schemaResponseField]: mockSample[schemaResponseField],
      };
      setPreviewSample(projected);
    } catch (e) {
      setPreviewError('Invalid file-list JSON.');
    }
  };

  const handleLaunch = () => {
    let fileList: string[] = [];
    try {
      fileList = JSON.parse(fileListJson);
      if (!Array.isArray(fileList) || fileList.length === 0) throw new Error('Empty file list');
    } catch (e) {
      alert('Invalid file-list JSON. Provide a JSON array of file paths.');
      return;
    }

    onLaunch({
      cdn_only: true,
      repo,
      date_folder: dateFolder,
      file_list: fileList,
      commit_cap_repo_index: targetRepoIndex,
      schema_projection: {
        prompt_field: schemaPromptField,
        response_field: schemaResponseField,
      },
      lightning: {
        reuse_running: true,
        machine: 'L40S',
        cloud_priority: ['lightning-lambda-prod', 'lightning-public-prod'],
      },
    });
  };

  return (
    <section className="p-4 border rounded space-y-3">
      <h3 className="font-semibold">HF CDN-bypass loader (no API auth)</h3>

      <label className="block text-sm">
        Dataset repo
        <input
          className="w-full border rounded px-2 py-1 text-sm"
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
          placeholder="datasets/airship/mirror-merged"
        />
      </label>

      <label className="block text-sm">
        Date folder
        <input
          className="w-full border rounded px-2 py-1 text-sm"
          value={dateFolder}
          onChange={(e) => setDateFolder(e.target.value)}
          placeholder="2026-04-29"
        />
      </label>

      <label className="block text-sm">
        File list (JSON from list_repo_tree)
        <textarea
          className="w-full border rounded px-2 py-1 text-sm font-mono text-xs"
          rows={4}
          value={fileListJson}
          onChange={(e) => setFileListJson(e.target.value)}
          placeholder={JSON.stringify(['batches/mirror-merged/2026-04-29/slug1.parquet'], null, 2)}
        />
      </label>

      <div className="flex gap-3 items-end">
        <label className="flex-1 text-sm">
          Prompt field
          <input
            className="w-full border rounded px-2 py-1 text-sm"
            value={schemaPromptField}
            onChange={(e) => setSchemaPromptField(e.target.value)}
          />
        </label>
        <label className="flex-1 text-sm">
          Response field
          <input
            className="w-full border rounded px-2 py-1 text-sm"
            value={schemaResponseField}
            onChange={(e) => setSchemaResponseField(e.target.value)}
          />
        </label>
      </div>

      <label className="block text-sm">
        Commit-cap repo index (0–4 for 5 siblings)
        <input
          className="w-full border rounded px-2 py-1 text-sm"
          type="number"
          min={0}
          max={4}
          value={targetRepoIndex}
          onChange={(e) => setTargetRepoIndex(Number(e.target.value))}
        />
      </label>

      <div className="flex gap-2">
        <button
          className="text-xs border rounded px-3 py-1"
          onClick={tryPreview}
          type="button"
        >
          Preview projection
        </button>
        {previewError && <div className="text-xs text-red-600">{previewError}</div>}
      </div>

      {previewSample && (
        <pre className="bg-gray-50 border rounded p-2 text-xs font-mono overflow-auto">
          {JSON.stringify(previewSample, null, 2)}
        </pre>
      )}

      <div className="text-xs text-gray-500">
        Tip: CDN downloads bypass HF API rate limits. Schema projection keeps only prompt/
