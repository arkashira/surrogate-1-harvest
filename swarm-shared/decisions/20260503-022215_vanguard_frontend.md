# vanguard / frontend

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest, non-redundant insights from both candidates and resolved contradictions in favor of correctness and concrete actionability.

### 1. Diagnosis (merged, tightened)
- **No persisted manifest**: every training UI action triggers authenticated `list_repo_tree`, burning HF API quota and risking 429.
- **No CDN bypass**: training/preview flows use authenticated fetches or `load_dataset(streaming=True)` on heterogeneous schemas, causing PyArrow CastError and slow UX.
- **No Studio reuse or idle-stop resilience**: frontend recreates Studios instead of listing/running existing ones and does not guard against Lightning idle-stop, wasting quota and killing runs.
- **No pre-list + embed workflow**: users cannot snapshot a `(repo, dateFolder)` file list once and embed CDN-only paths for zero-API training runs.

### 2. Proposed change (merged)
Add a frontend manifest + Studio orchestration layer that:
- Persists `(repo, dateFolder) → file-list` after one authenticated list (with TTL and size cap).
- Uses CDN-bypass URLs for previews and training file lists.
- Exposes `getTrainingManifest(repo, dateFolder)` returning CDN-only paths for Lightning scripts.
- Reuses running/stopped Studios and guards against idle-stop.
- Adds a small UI helper in the training config panel to show cached manifest status and allow refresh.

Scope:
- Create `/opt/axentx/vanguard/src/services/hfManifest.ts`
- Create `/opt/axentx/vanguard/src/services/lightningStudio.ts`
- Create `/opt/axentx/vanguard/src/services/dataLoader.ts`
- Add UI helper in training config panel for manifest status/refresh.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/src/services
```

`/opt/axentx/vanguard/src/services/hfManifest.ts`
```ts
const MF_KEY = (repo: string, dateFolder: string) =>
  `vanguard:hf:manifest:${repo}:${dateFolder}`;

const TTL_MS = 24 * 3600 * 1000; // 1 day
const MAX_FILES = 5000;
const MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024; // 2 GB

export interface HfFile {
  path: string;
  size: number;
  cdnUrl: string;
}

export interface HfManifest {
  repo: string;
  dateFolder: string;
  createdAt: number;
  files: HfFile[];
}

function isStale(createdAt: number) {
  return Date.now() - createdAt > TTL_MS;
}

export async function fetchAndPersistManifest(
  repo: string,
  dateFolder: string,
  token: string
): Promise<HfManifest> {
  const res = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(
      dateFolder
    )}&recursive=false`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }
  );

  if (!res.ok) {
    throw new Error(`HF tree list failed: ${res.status}`);
  }

  const items = await res.json();
  const files: HfFile[] = (items || [])
    .filter((it: any) => it.type === "file")
    .map((it: any) => ({
      path: `${dateFolder}/${it.path}`.replace(/\/+/g, "/"),
      size: Math.max(0, Number(it.size) || 0),
      cdnUrl: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(
        dateFolder
      )}/${encodeURIComponent(it.path)}`,
    }));

  const totalSize = files.reduce((s, f) => s + f.size, 0);
  if (files.length > MAX_FILES) {
    throw new Error(`Too many files (${files.length} > ${MAX_FILES})`);
  }
  if (totalSize > MAX_TOTAL_BYTES) {
    throw new Error(`Total size too large (${totalSize} > ${MAX_TOTAL_BYTES})`);
  }

  const manifest: HfManifest = {
    repo,
    dateFolder,
    createdAt: Date.now(),
    files,
  };

  try {
    localStorage.setItem(MF_KEY(repo, dateFolder), JSON.stringify(manifest));
  } catch (err) {
    console.warn("Could not persist manifest to localStorage", err);
  }
  return manifest;
}

export function getPersistedManifest(
  repo: string,
  dateFolder: string
): HfManifest | null {
  try {
    const raw = localStorage.getItem(MF_KEY(repo, dateFolder));
    if (!raw) return null;
    const m = JSON.parse(raw) as HfManifest;
    if (!m || !m.repo || !Array.isArray(m.files) || isStale(m.createdAt)) {
      return null;
    }
    return m;
  } catch {
    return null;
  }
}

// Returns CDN-only paths suitable for embedding in Lightning train.py
export function getCdnFileList(
  repo: string,
  dateFolder: string
): string[] | null {
  const m = getPersistedManifest(repo, dateFolder);
  if (!m) return null;
  return m.files.map((f) => f.cdnUrl);
}

export function clearManifest(repo: string, dateFolder: string) {
  try {
    localStorage.removeItem(MF_KEY(repo, dateFolder));
  } catch {}
}
```

`/opt/axentx/vanguard/src/services/lightningStudio.ts`
```ts
// Adjust import to actual Lightning SDK
declare const Lightning: any;

export type Machine = "L40S" | "A100" | "A10G" | "T4";

export interface Studio {
  id: string;
  name: string;
  status: "Running" | "Stopped" | "Pending" | "Error";
  machine: Machine;
  start: (opts?: { machine?: Machine }) => Promise<void>;
  stop: () => Promise<void>;
  run: (command: string[], opts?: { env?: Record<string, string> }) => Promise<any>;
}

export async function listStudios(): Promise<Studio[]> {
  // Adjust to real SDK call
  return (await Lightning.Teamspace.studios()) || [];
}

export async function getOrCreateStudio(
  name: string,
  machine: Machine = "L40S"
): Promise<Studio> {
  const studios = await listStudios();

  const running = studios.find(
    (s) => s.name === name && s.status === "Running"
  );
  if (running) return running;

  const stopped = studios.find(
    (s) => s.name === name && s.status === "Stopped"
  );
  if (stopped) {
    await stopped.start({ machine });
    return stopped;
  }

  return Lightning.Studio.create({ name, machine });
}

export async function runOnStudio(
  studio: Studio,
  command: string[],
  env?: Record<string, string>
) {
  // Guard against idle-stop
  if (studio.status !== "Running") {
    await studio.start({ machine: (studio as any).machine });
  }
  return studio.run(command, { env });
}
```

`/opt/axentx/vanguard/src/services/dataLoader.ts`
```ts
import { getPersistedManifest } from "./hfManifest";

export async function loadFilePreview(
  repo: string,
  dateFolder: string,
  filePath: string,
  options?: { maxBytes?: number }
): Promise<ArrayBuffer> {
  const cdnUrl = `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(
    dateFolder
  )}/${encodeURIComponent(filePath)}`;
  const res = await fetch(cdnUrl);
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  const blob = await res.blob();
  if (options?.maxBytes && blob.size > options.maxBytes) {
    throw new Error("File exceeds preview size limit");
  }
  return blob.arrayBuffer();
}

export function getManifestSummary(repo: string, dateFolder: string) {
  const m
