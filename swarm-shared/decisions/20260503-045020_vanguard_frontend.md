# vanguard / frontend

## 1. Diagnosis
- No frontend manifest for CDN-backed dataset fetches → training UI triggers HF API `list_repo_tree`/`load_dataset` at runtime and hits 429s.
- Missing date-folder content-addressed file list → epochs are non-reproducible and shard order drifts across runs.
- Data loader in UI uses heavy client-side JSONL parsing instead of streaming Parquet projection → slow TTI and high memory on low-end devices.
- No fallback to CDN bypass (`resolve/main/`) when API rate-limited → training dashboard stalls instead of degrading gracefully.
- Hardcoded repo/date in UI → requires rebuild to switch daily mirror folders; no runtime config.

## 2. Proposed change
Add a lightweight frontend manifest loader and CDN-backed data fetcher scoped to:
- `src/lib/data/` (new)
- `src/routes/training/+page.svelte` (or equivalent route)
- `src/app.html` or `src/routes/+layout.svelte` for config provider

## 3. Implementation

```bash
# create directory
mkdir -p /opt/axentx/vanguard/src/lib/data
```

`src/lib/data/manifest.ts`
```ts
// Lightweight manifest loader + CDN fetcher
// Uses CDN bypass: https://huggingface.co/datasets/{repo}/resolve/main/{path}
// Expects manifest JSON from Mac orchestration: { date: string, files: string[] }

const HF_DATASETS_REPO = import.meta.env.VITE_HF_DATASETS_REPO || 'axentx/surrogate-1';
const CDN_ROOT = `https://huggingface.co/datasets/${HF_DATASETS_REPO}/resolve/main`;

export interface Manifest {
  date: string;
  files: string[];
}

export async function loadManifest(date: string): Promise<Manifest> {
  const res = await fetch(`${CDN_ROOT}/batches/mirror-merged/${date}/manifest.json`, {
    cache: 'no-cache'
  });
  if (!res.ok) throw new Error(`Manifest load failed: ${res.status}`);
  return res.json();
}

export async function* streamParquetProjected(date: string, files: string[]) {
  // Project {prompt,response} from Parquet via CDN (no HF API calls).
  // Uses fetch + streaming decompression via Apache Arrow WASM (or fallback to JSONL).
  // For MVP: fetch each file and yield parsed rows.
  for (const file of files) {
    const url = `${CDN_ROOT}/batches/mirror-merged/${date}/${file}`;
    try {
      const res = await fetch(url, { cache: 'no-cache' });
      if (!res.ok) continue;
      const buf = await res.arrayBuffer();
      // Minimal projection: if parquet -> use arrow-wasm (optional heavy dep).
      // Fallback: assume NDJSON lines for now (mirror can produce both).
      const text = new TextDecoder().decode(buf);
      const lines = text.trim().split('\n');
      for (const line of lines) {
        if (!line) continue;
        try {
          const obj = JSON.parse(line);
          // Project only required fields
          yield {
            prompt: obj.prompt || obj.messages?.[0]?.content || '',
            response: obj.response || obj.messages?.[1]?.content || ''
          };
        } catch {
          // skip malformed
        }
      }
    } catch (e) {
      console.warn('Failed to fetch', file, e);
    }
  }
}

export async function loadCdnDataset(date: string) {
  const manifest = await loadManifest(date);
  return {
    date: manifest.date,
    files: manifest.files,
    stream: () => streamParquetProjected(date, manifest.files)
  };
}
```

`src/routes/+layout.svelte` (or nearest layout)
```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { loadCdnDataset } from '$lib/data/manifest';

  let datasetInfo: { date: string; files: string[] } | null = null;
  let rows: Array<{ prompt: string; response: string }> = [];

  onMount(async () => {
    // Default to today's date folder; can be parameterized via route
    const today = new Date().toISOString().slice(0, 10);
    try {
      const ds = await loadCdnDataset(today);
      datasetInfo = { date: ds.date, files: ds.files };
      for await (const row of ds.stream()) {
        rows.push(row);
        // Yield to UI periodically for responsiveness
        if (rows.length % 50 === 0) await new Promise(r => setTimeout(r, 0));
      }
    } catch (e) {
      console.error(e);
    }
  });
</script>

<slot {datasetInfo} {rows} />
```

`vite.config.ts` (add env)
```ts
import { defineConfig } from 'vite';
import { sveltekit } from '@sveltejs/kit/vite';

export default defineConfig({
  plugins: [sveltekit()],
  define: {
    'import.meta.env.VITE_HF_DATASETS_REPO': JSON.stringify(process.env.VITE_HF_DATASETS_REPO || 'axentx/surrogate-1')
  }
});
```

## 4. Verification
- Place a test manifest at `batches/mirror-merged/2026-05-03/manifest.json` in the HF repo with a small list of NDJSON files containing `{prompt,response}`.
- Run dev server: `npm run dev` and open the training route.
- Confirm network tab shows CDN fetches to `resolve/main/...` with no Authorization header and no HF API `/api/` calls.
- Confirm rows appear in UI and epoch order is stable on refresh.
- Simulate API rate-limit by blocking `api.huggingface.co` in devtools; UI should still load via CDN.
