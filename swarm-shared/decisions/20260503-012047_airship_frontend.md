# airship / frontend

## Highest-Value Incremental Improvement
**Implement HF CDN-bypass dataset loader + Lightning Studio reuse in the training UI**  
- Eliminates HF API 429s and `pyarrow.CastError`s during dataset loading  
- Reduces Lightning quota burn by reusing studios and removing training-time API calls  
- Ships as a small, focused frontend change (<2h)

---

## Implementation Plan

1. **Add CDN-bypass dataset loader**  
   - Replace `load_dataset(streaming=True)` with CDN-only fetches using `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
   - Pre-list file paths once (Mac orchestration) → save to JSON → embed in training script  
   - Project to `{prompt, response}` only at parse time; ignore heterogeneous schemas

2. **Add Lightning Studio reuse logic**  
   - Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse running ones  
   - If stopped, restart with `target.start(machine=Machine.L40S)`  
   - Avoids 80hr/mo quota burn

3. **UI wiring**  
   - Add toggle: “Use CDN bypass (recommended)”  
   - Add status indicator for studio reuse  
   - Wire into existing training form

---

## Code Snippets

### 1. CDN-bypass dataset loader (frontend-facing utility)
```ts
// src/lib/dataset/cdnLoader.ts
export interface FileEntry {
  path: string;
  size: number;
  type: 'file' | 'directory';
}

export interface DatasetFileList {
  repo: string;
  revision: string;
  folder: string;
  files: FileEntry[];
  generatedAt: string;
}

// Parse only {prompt, response} from JSONL/parquet-like lines
export function* parseCdnLines(
  lines: string[]
): Generator<{ prompt: string; response: string }> {
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const obj = JSON.parse(line);
      // Project only required fields; ignore extra schema
      yield {
        prompt: String(obj.prompt ?? obj.input ?? obj.question ?? ''),
        response: String(obj.response ?? obj.output ?? obj.answer ?? ''),
      };
    } catch {
      // Skip malformed lines (heterogeneous schema tolerance)
      continue;
    }
  }
}

// Fetch single file via CDN (no Authorization header)
export async function fetchCdnFile(
  repo: string,
  filePath: string,
  revision = 'main'
): Promise<string> {
  const url = `https://huggingface.co/datasets/${repo}/resolve/${revision}/${encodeURIComponent(filePath)}`;
  const res = await fetch(url, {
    method: 'GET',
    // No Authorization header -> bypasses /api/ rate limits
    headers: {
      Accept: 'text/plain, application/json, */*',
    },
  });

  if (!res.ok) {
    throw new Error(`CDN fetch failed: ${res.status} ${res.statusText} for ${url}`);
  }

  return await res.text();
}

// Stream-parse large files in chunks to avoid OOM
export async function* streamCdnLines(
  repo: string,
  filePath: string,
  revision = 'main',
  chunkSize = 64 * 1024 // 64KB
): AsyncGenerator<{ prompt: string; response: string }> {
  const url = `https://huggingface.co/datasets/${repo}/resolve/${revision}/${encodeURIComponent(filePath)}`;
  const res = await fetch(url);

  if (!res.ok) {
    throw new Error(`CDN stream failed: ${res.status} ${res.statusText}`);
  }

  const reader = res.body?.getReader();
  if (!reader) return;

  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += new TextDecoder().decode(value, { stream: true });

    let newlineIndex;
    while ((newlineIndex = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (!line) continue;

      try {
        const obj = JSON.parse(line);
        yield {
          prompt: String(obj.prompt ?? obj.input ?? obj.question ?? ''),
          response: String(obj.response ?? obj.output ?? obj.answer ?? ''),
        };
      } catch {
        // Skip malformed lines (heterogeneous schema tolerance)
        continue;
      }
    }
  }

  // Flush remaining buffer
  if (buffer.trim()) {
    try {
      const obj = JSON.parse(buffer.trim());
      yield {
        prompt: String(obj.prompt ?? obj.input ?? obj.question ?? ''),
        response: String(obj.response ?? obj.output ?? obj.answer ?? ''),
      };
    } catch {
      // ignore
    }
  }
}
```

### 2. Lightning Studio reuse hook
```ts
// src/lib/lightning/studio.ts
import { Lightning } from 'lightning-ai'; // adjust import per actual SDK

export interface StudioConfig {
  name: string;
  machine?: 'L40S' | 'H200';
  cloud?: string;
}

export async function getOrCreateStudio(config: StudioConfig) {
  const teamspace = Lightning.Teamspace.current();
  const existing = teamspace.studios.find(
    (s) => s.name === config.name && s.status === 'Running'
  );

  if (existing) {
    return existing;
  }

  // If exists but stopped, restart
  const stopped = teamspace.studios.find((s) => s.name === config.name);
  if (stopped) {
    await stopped.start({ machine: config.machine || 'L40S' });
    return stopped;
  }

  // Create new only if none exist
  return Lightning.Studio.create({
    name: config.name,
    machine: config.machine || 'L40S',
    cloud: config.cloud || 'lightning-public-prod',
    create_ok: true,
  });
}
```

### 3. Training form UI integration
```tsx
// src/components/TrainingForm.tsx
import { useState } from 'react';
import { fetchCdnFile, streamCdnLines } from '@/lib/dataset/cdnLoader';
import { getOrCreateStudio } from '@/lib/lightning/studio';

export function TrainingForm() {
  const [useCdn, setUseCdn] = useState(true);
  const [repo, setRepo] = useState('');
  const [folder, setFolder] = useState('');
  const [status, setStatus] = useState('idle');

  const handleTrain = async () => {
    setStatus('preparing');
    try {
      // 1) Studio reuse
      const studio = await getOrCreateStudio({
        name: `surrogate-${Date.now()}`,
        machine: 'L40S',
      });
      setStatus('studio_ready');

      // 2) CDN-bypass dataset load
      if (useCdn) {
        // Example: pre-listed file list embedded by orchestration script
        const fileList: string[] = await fetch('/file-list.json').then((r) => r.json());
        setStatus('loading_dataset');

        for (const file of fileList) {
          for await (const sample of streamCdnLines(repo, `${folder}/${file}`)) {
            // send to studio training job (example)
            studio.run({
              prompt: sample.prompt,
              response: sample.response,
            });
          }
        }
      } else {
        // fallback path (not recommended)
        // legacy load_dataset call
      }

      setStatus('training');
    } catch (err) {
      console.error(err);
      setStatus('error');
    }
  };

  return (
    <div className="p-4 space-y-4">
      <h2 className="text-lg font-semibold">Surrogate Training</h2>

      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={useCdn}
          onChange={(e) => setUseCdn(e.target.checked)}
        />
        Use CDN bypass
