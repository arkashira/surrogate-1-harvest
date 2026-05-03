# airship / frontend

## Highest-Value Incremental Improvement
**Implement HF CDN-bypass dataset loader + Lightning Studio reuse in the training UI**  
- Eliminates HF API 429s and `pyarrow.CastError`s during dataset loading  
- Reduces Lightning quota burn by re-running in existing Running studios  
- Ships in <2h and unblocks reliable surrogate training from the frontend

---

## Implementation Plan (airship frontend)

1. Add a training config panel (React) to:
   - Accept HF dataset repo + date folder
   - Trigger Mac-side `list_repo_tree` (once) and upload `file-list.json` (or paste it)
   - Choose Lightning machine (L40S by default) and enable studio reuse
2. Create `src/lib/hf-cdn-loader.ts`:
   - Build file URLs via `https://huggingface.co/datasets/{repo}/resolve/main/{date}/{file}`
   - Stream/parquet projection to `{prompt,response}` only (ignore mixed schema)
   - Zero Authorization header (CDN bypass)
3. Create `src/lib/lightning-studio.ts`:
   - `listRunningStudios()` → reuse by name if Running
   - If stopped, restart with `Machine.L40S`
   - Fallback to `Studio(create_ok=True)` only if none exist
4. Wire into training page:
   - Show file list preview
   - Start training job via Lightning SDK (calls backend launcher)
   - Status polling with auto-restart on idle-stop
5. Add small inline docs referencing HF CDN bypass and quota-saving patterns

---

## Code Snippets

### Frontend: HF CDN loader (project to {prompt,response})
```ts
// src/lib/hf-cdn-loader.ts
export interface HFCdnFile {
  repo: string;   // e.g. "myorg/surrogate-mirror"
  date: string;   // e.g. "2026-04-29"
  path: string;   // relative path within date folder
}

export async function* streamCdnParquet(
  file: HFCdnFile,
  signal?: AbortSignal
): AsyncGenerator<{ prompt: string; response: string }> {
  const url = `https://huggingface.co/datasets/${file.repo}/resolve/main/${file.date}/${encodeURIComponent(
    file.path
  )}`;

  // CDN bypass: no Authorization header
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);

  // In browser we can't parse parquet directly; this function is intended
  // to be used via a backend proxy or WebWorker with parquet-wasm.
  // For frontend demo we assume a line-delimited JSONL fallback or
  // delegate to backend. Keep signature for consistency.
  //
  // Production path: call backend /hf-cdn/proxy which streams parquet
  // and yields {prompt,response}.
  const body = res.body;
  if (!body) return;

  // Placeholder: delegate to backend proxy
  const proxyRes = await fetch("/api/hf-cdn/proxy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
    signal,
  });
  if (!proxyRes.ok) throw new Error(`Proxy failed: ${proxyRes.status}`);

  const reader = proxyRes.body?.getReader();
  if (!reader) return;

  // Expect NDJSON from proxy: {prompt,response}
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const obj = JSON.parse(line);
        if (obj.prompt != null && obj.response != null) {
          yield { prompt: String(obj.prompt), response: String(obj.response) };
        }
      } catch {
        // skip malformed
      }
    }
  }
}
```

### Frontend: Lightning Studio reuse
```ts
// src/lib/lightning-studio.ts
import { Lightning, Teamspace, Studio, Machine } from "lightning-sdk"; // pseudo import — adapt to real SDK

export async function getOrCreateStudio(opts: {
  name: string;
  machine?: Machine;
  createOk?: boolean;
}) {
  const { name, machine = Machine.L40S, createOk = true } = opts;

  // Reuse running studio
  const studios = await Teamspace.studios();
  const running = studios.find((s) => s.name === name && s.status === "Running");
  if (running) {
    console.log(`Reusing running studio: ${name}`);
    return running;
  }

  // If exists but stopped, restart
  const stopped = studios.find((s) => s.name === name);
  if (stopped) {
    console.log(`Restarting stopped studio: ${name}`);
    await stopped.start({ machine });
    return stopped;
  }

  if (!createOk) throw new Error(`No studio found: ${name}`);
  console.log(`Creating studio: ${name}`);
  return Studio({ name, create_ok: true, machine });
}

export async function runInStudio(
  studioName: string,
  scriptPath: string,
  args: string[] = []
) {
  const studio = await getOrCreateStudio({ name: studioName });
  // Ensure studio is running before run
  if (studio.status !== "Running") {
    await studio.start({ machine: Machine.L40S });
  }

  const run = await studio.run({
    target: scriptPath,
    args,
  });
  return run;
}
```

### Frontend: Training config panel (minimal)
```tsx
// src/components/TrainingConfig.tsx
import { useState } from "react";
import { getOrCreateStudio, runInStudio } from "../lib/lightning-studio";

export default function TrainingConfig() {
  const [repo, setRepo] = useState("myorg/surrogate-mirror");
  const [date, setDate] = useState("2026-04-29");
  const [studioName, setStudioName] = useState("surrogate-trainer");
  const [status, setStatus] = useState<string | null>(null);

  const startTraining = async () => {
    setStatus("Preparing studio...");
    try {
      const studio = await getOrCreateStudio({ name: studioName });
      setStatus(`Studio: ${studio.status}. Starting training...`);
      // Backend launcher should use HF CDN bypass + file-list.json
      const run = await runInStudio(studioName, "train.py", [
        "--repo",
        repo,
        "--date",
        date,
        "--use-cdn-bypass",
      ]);
      setStatus(`Run started: ${run.id}`);
    } catch (err: any) {
      setStatus(`Error: ${err.message}`);
    }
  };

  return (
    <div className="p-4 border rounded">
      <h3 className="font-bold mb-2">Surrogate Training (HF CDN bypass)</h3>
      <label className="block mb-1">Dataset repo</label>
      <input
        className="border p-1 w-full mb-2"
        value={repo}
        onChange={(e) => setRepo(e.target.value)}
      />
      <label className="block mb-1">Date folder</label>
      <input
        className="border p-1 w-full mb-2"
        value={date}
        onChange={(e) => setDate(e.target.value)}
      />
      <label className="block mb-1">Studio name</label>
      <input
        className="border p-1 w-full mb-2"
        value={studioName}
        onChange={(e) => setStudioName(e.target.value)}
      />
      <button
        className="bg-blue-600 text-white px-4 py-2 rounded"
        onClick={startTraining}
      >
        Start Training
      </button>
      {status && <p className="mt-2 text-sm">{status}</p>}
      <p
