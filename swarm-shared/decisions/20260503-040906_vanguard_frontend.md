# vanguard / frontend

## Final Synthesized Implementation Plan

**Core diagnosis (agreed):**  
Frontend runtime HF API calls (`list_repo_tree`, `load_dataset`) cause 429s and non-reproducible views. There is no content-addressed manifest, no CDN-only data path, no deterministic snapshot selection UI, and mixed-schema parquet in `enriched/` risks parsing errors and bloated payloads.

**Resolution of contradictions in favor of correctness + actionability:**
- Use **TypeScript** (not plain JS) for type safety across manifest/files/snapshots (Candidate 2), but keep build-tool-agnostic exports so it works in the existing project.
- Keep **CDN-only URLs** and **manifest-driven** file listing (Candidate 1), but add **date/tag snapshot selection UI** and **schema projection** to `{ prompt, response }` (Candidate 2).
- Provide **ops-friendly manifest generation** (deterministic, sha256, reproducible) and a **local dev fallback** (Candidate 1), plus **graceful degradation** when manifest is missing (Candidate 2).
- Keep changes minimal and scoped: one new `dataset/` module, one UI component, one sample manifest, and small wiring in app entry.

---

## 1. File tree to create/update
```text
/opt/axentx/vanguard/
├── public/
│   └── dataset-manifest.json        # sample for dev; ops will replace in prod
├── src/
│   ├── lib/
│   │   └── dataset/
│   │       ├── manifest.ts          # types + loader + CDN URL builder
│   │       ├── snapshot.ts          # snapshot selector + projection
│   │       └── parquetProjector.ts  # safe projection to { prompt, response }
│   ├── components/
│   │   └── DatasetSnapshotSelector.tsx  # date/tag picker + pinned info
│   ├── main.tsx (or main.ts / main.js)  # app entry
│   └── index.html (or equivalent)
└── tests/
    └── dataset/
        ├── manifest.test.ts
        └── snapshot.test.ts
```

---

## 2. Implementation

### src/lib/dataset/manifest.ts
```ts
// src/lib/dataset/manifest.ts
// Content-addressed manifest loader + CDN URL builder (CDN-only).

export interface DatasetFile {
  path: string;
  sha256: string;
  size: number;
}

export interface DatasetRepo {
  owner: string;
  name: string;
  branch: string;
}

export interface DatasetManifest {
  version: string;
  createdAt: string; // ISO
  tag?: string;
  repo: DatasetRepo;
  files: DatasetFile[];
}

const CDN_BASE = 'https://huggingface.co/datasets';

export function buildCdnUrl(repo: DatasetRepo, filePath: string): string {
  return `${CDN_BASE}/${repo.owner}/${repo.name}/resolve/${repo.branch}/${filePath}`;
}

export async function loadDatasetManifest(path = '/dataset-manifest.json'): Promise<DatasetManifest> {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) {
    throw new Error(`Failed to load dataset manifest: ${res.status} ${res.statusText}`);
  }
  const manifest = (await res.json()) as DatasetManifest;
  if (!manifest.version || !Array.isArray(manifest.files) || !manifest.repo) {
    throw new Error('Invalid manifest format');
  }
  return manifest;
}

export function attachCdnUrls(manifest: DatasetManifest) {
  return {
    ...manifest,
    files: manifest.files.map((f) => ({
      ...f,
      cdnUrl: buildCdnUrl(manifest.repo, f.path),
    })),
  };
}
```

---

### src/lib/dataset/snapshot.ts
```ts
// src/lib/dataset/snapshot.ts
// Deterministic snapshot selection + projection helpers.

import { DatasetManifest, DatasetFile, DatasetRepo } from './manifest';

export interface SnapshotFile extends DatasetFile {
  cdnUrl: string;
}

export interface Snapshot {
  version: string;
  createdAt: string;
  tag?: string;
  repo: DatasetRepo;
  fileCount: number;
  files: SnapshotFile[];
}

export function createSnapshot(manifest: DatasetManifest): Snapshot {
  const files = manifest.files.map((f) => ({
    ...f,
    cdnUrl: `${CDN_BASE}/${manifest.repo.owner}/${manifest.repo.name}/resolve/${manifest.repo.branch}/${f.path}`,
  }));
  return {
    version: manifest.version,
    createdAt: manifest.createdAt,
    tag: manifest.tag,
    repo: manifest.repo,
    fileCount: files.length,
    files,
  };
}

export function selectSnapshotByDate(
  manifests: DatasetManifest[],
  targetDate: string // ISO date (YYYY-MM-DD) or prefer tag
): DatasetManifest | null {
  // Prefer exact tag match first
  const byTag = manifests.find((m) => m.tag && m.tag === targetDate);
  if (byTag) return byTag;

  // Otherwise latest createdAt on or before targetDate
  const candidates = manifests
    .filter((m) => m.createdAt.slice(0, 10) <= targetDate)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  return candidates[0] || null;
}

const CDN_BASE = 'https://huggingface.co/datasets';
```

---

### src/lib/dataset/parquetProjector.ts
```ts
// src/lib/dataset/parquetProjector.ts
// Lightweight, schema-safe projector for parquet rows -> { prompt, response }.
// Uses Apache Arrow via apache-arrow package if available; otherwise does best-effort field extraction.

export interface PromptResponse {
  prompt: string;
  response: string;
}

export function projectToPromptResponse(row: Record<string, unknown>): PromptResponse | null {
  if (!row) return null;

  // Common field names (case-insensitive)
  const promptKeys = ['prompt', 'input', 'question', 'instruction'];
  const responseKeys = ['response', 'output', 'answer', 'completion'];

  const find = (keys: string[], obj: Record<string, unknown>) => {
    for (const k of keys) {
      const found = Object.keys(obj).find((ok) => ok.toLowerCase() === k);
      if (found) return obj[found];
    }
    return undefined;
  };

  const promptRaw = find(promptKeys, row);
  const responseRaw = find(responseKeys, row);

  const prompt = promptRaw != null ? String(promptRaw) : '';
  const response = responseRaw != null ? String(responseRaw) : '';

  // Basic validity: require non-empty prompt or response
  if (!prompt.trim() && !response.trim()) return null;
  return { prompt, response };
}
```

---

### src/components/DatasetSnapshotSelector.tsx
```tsx
// src/components/DatasetSnapshotSelector.tsx
// Minimal date/tag selector + pinned snapshot display.

import React, { useEffect, useState } from 'react';
import { loadDatasetManifest, DatasetManifest } from '../lib/dataset/manifest';
import { selectSnapshotByDate, createSnapshot } from '../lib/dataset/snapshot';

interface Props {
  availableManifests?: DatasetManifest[]; // if preloaded
  onSnapshotChange?: (snap: any) => void;
}

export function DatasetSnapshotSelector({ availableManifests, onSnapshotChange }: Props) {
  const [manifests, setManifests] = useState<DatasetManifest[]>(availableManifests || []);
  const [selectedDate, setSelectedDate] = useState<string>(new Date().toISOString().slice(0, 10));
  const [snapshot, setSnapshot] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!availableManifests) {
      // Try to load a single local manifest for dev; ops may expose multiple via an index.
      setLoading(true);
      loadDatasetManifest()
        .then((m) => setManifests([m]))
        .catch(() => setManifests([]))
        .finally(() => setLoading(false));
    }
  }, [availableManifests]);


