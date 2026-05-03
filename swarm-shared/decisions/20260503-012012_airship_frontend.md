# airship / frontend

## Final consolidated implementation (frontend-only, <2h)

**Goal** (unchanged)  
- Eliminate HF API 429s and `pyarrow.CastError`s during dataset loading.  
- Reduce Lightning quota burn by reusing studios and removing training-time API calls.  
- Keep all changes frontend + Mac orchestration (no backend changes).

**Chosen approach** (merged, corrected, actionable)  
- **HF CDN-bypass loader**: fetch parquet files via `/resolve/main/` (no auth, high rate).  
- **File-list pre-generation** (Mac): list repo tree once per date folder, save JSON; training uses CDN-only URLs.  
- **Studio reuse**: attach to a running studio; restart if stopped; create only if absent.  
- **Schema projection at parse time**: select only `prompt` and `response` columns to prevent mixed-schema `pyarrow.CastError`.  
- **Streaming/batch fetch**: avoid loading full dataset into browser memory; stream rows to training config or pass buffers to backend.

---

## Implementation plan (frontend + orchestration)

### 1) HF CDN-bypass dataset loader (frontend)
- Create `src/lib/dataset/hf-cdn-loader.ts`.
- Accept repo, date folder, and file-list JSON (generated on Mac).
- Build URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{date}/{file}`.
- Fetch with `credentials: "omit"`; stream rows and project to `{prompt, response}`.
- Use a WASM parquet reader (e.g., `parquet-wasm` or `@hypnosphi/parquetjs-wasm`) to select only required columns and avoid `pyarrow.CastError`.
- Stream rows to training config or pass file buffers to the training backend (do not accumulate full dataset in memory).

### 2) Studio reuse helper (frontend)
- Create `src/lib/lightning/studio-reuse.ts`.
- On training start:
  1. List studios in current teamspace.
  2. If a studio with target name is **Running**, attach to it.
  3. If **Stopped**, restart with `machine="L40S"` (prefer free tier; use H200 only in lambda-prod).
  4. If absent, create with `create_ok=true` and deterministic name.
- Return studio handle for training job submission.

### 3) File-list generator (Mac orchestration script)
- Create `scripts/generate-hf-file-list.ts`.
- Run once per date folder after rate-limit window clears.
- Use HF API `list_repo_tree(path, recursive=false)` to list parquet files.
- Save to `file-lists/file-list-{date}.json` with entries like `{ path: "2026-05-03/file-001.parquet" }`.
- Embed path to this JSON in training UI config.

### 4) Update training UI
- Add file-list upload/selector to training form.
- On submit:
  1. Call Studio reuse helper.
  2. Start training using CDN loader and selected file list.
  3. Show status: “Using CDN bypass (0 API calls during training)”.
- Ensure training config references CDN URLs and selected columns only.

### 5) Tests & validation
- Verify CDN URLs resolve without auth and return expected parquet files.
- Verify reused studio attaches or restarts correctly.
- Verify no `pyarrow.CastError` by selecting only `prompt` and `response` columns during parse.
- Verify training starts and consumes data without HF API calls (check network tab for `/api/` calls during data load).

---

## Code snippets

### `src/lib/dataset/hf-cdn-loader.ts`
```ts
// HF CDN-bypass dataset loader (frontend-only)
// Uses /resolve/main/ to avoid /api/ rate limits.
// Projects to {prompt, response} only to avoid pyarrow.CastError.

export interface CdnFileEntry {
  path: string; // relative to repo root, e.g. "2026-05-03/file-001.parquet"
}

export interface CdnDatasetConfig {
  repo: string; // e.g. "org/surrogate-data"
  dateFolder: string; // e.g. "2026-05-03"
  fileList: CdnFileEntry[]; // from file-list-{date}.json
  batchSize?: number;
}

// Lightweight row type expected by training
export type TrainingRow = { prompt: string; response: string };

// Stream rows from CDN-hosted parquet files, projecting only prompt/response.
// Uses a WASM parquet reader to select columns and avoid mixed-schema errors.
export async function* streamCdnDataset(
  config: CdnDatasetConfig
): AsyncGenerator<TrainingRow> {
  const { repo, dateFolder, fileList } = config;
  const baseUrl = `https://huggingface.co/datasets/${repo}/resolve/main/${dateFolder}`;

  for (const entry of fileList) {
    const url = `${baseUrl}/${encodeURIComponent(entry.path)}`;
    const res = await fetch(url, { credentials: "omit" });
    if (!res.ok) {
      console.warn(`CDN fetch failed: ${url}`, res.status);
      continue;
    }

    const buffer = await res.arrayBuffer();

    // Use a WASM parquet reader to select only prompt/response columns.
    // Example using parquet-wasm (install separately):
    // import { ParquetReader } from "parquet-wasm/arrow1";
    // const table = ParquetReader.readTable(new Uint8Array(buffer), { columns: ["prompt", "response"] });
    // for (let i = 0; i < table.numRows; i++) {
    //   yield { prompt: table.getChild("prompt")?.get(i) ?? "", response: table.getChild("response")?.get(i) ?? "" };
    // }

    // Placeholder: replace with real columnar projection as above.
    const rows = parseParquetProjected(buffer);
    for (const row of rows) {
      yield { prompt: row.prompt ?? "", response: row.response ?? "" };
    }
  }
}

// Minimal placeholder — replace with real parquet projection (e.g., parquet-wasm)
function parseParquetProjected(buffer: ArrayBuffer): Array<{ prompt?: string; response?: string }> {
  // In production, use a WASM parquet reader and select only prompt/response columns.
  // Returning empty array here to avoid heavy dep in snippet.
  return [];
}
```

### `src/lib/lightning/studio-reuse.ts`
```ts
import { Lightning } from "@lightningai/sdk"; // adjust import to actual SDK

export interface StudioReuseOptions {
  name: string;
  machine?: "L40S" | "H200"; // prefer L40S for free tier; H200 only in lambda-prod
}

export async function reuseOrCreateStudio(opts: StudioReuseOptions) {
  const { name, machine = "L40S" } = opts;
  const teamspace = Lightning.Teamspace.current();

  // Reuse running studio
  for (const s of teamspace.studios) {
    if (s.name === name && s.status === "Running") {
      console.log(`Reusing running studio: ${name}`);
      return s;
    }
  }

  // Restart stopped studio
  for (const s of teamspace.studios) {
    if (s.name === name && s.status === "Stopped") {
      console.log(`Restarting stopped studio: ${name}`);
      await s.start({ machine });
      return s;
    }
  }

  // Create new (reuse name)
  console.log(`Creating new studio: ${name}`);
  const studio = await Lightning.Studio.create({
    name,
    machine,
    create_ok: true,
  });
  return studio;
}
```

### `scripts/generate-hf-file-list.ts` (Mac orchestration)
```ts
#!/usr/bin/env tsx
// Run from Mac to generate file-list JSON once per date folder.
// Uses HF API (list_repo_tree) — call only after rate-limit window clears.
// Output: file-list-{date}.json

import { HuggingFaceApi } from "hf-api-client"; // adjust to actual client
import fs from "fs";
import path from "path";

const REPO = "org/surrogate-data";
const DATE_FOLDER = process.argv[2] || "2026-0
