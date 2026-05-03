# vanguard / frontend

## Final Synthesized Implementation

After reconciling both proposals, the optimal solution combines **deterministic build-time manifests** (Candidate 2) with **runtime CDN caching and Lightning Studio reuse** (Candidate 1). This eliminates HF API calls at runtime, prevents 429s, and avoids duplicate Studio creation.

### 1. Directory Setup
```bash
mkdir -p /opt/axentx/vanguard/src/frontend/utils
mkdir -p /opt/axentx/vanguard/src/frontend/services
mkdir -p /opt/axentx/vanguard/src/data
mkdir -p /opt/axentx/vanguard/src/orchestration
```

### 2. Build-Time Manifest Generator (`src/data/manifest.ts`)
*Runs on Mac orchestration node to produce static JSON per `{date}/{slug}`*

```ts
// @/data/manifest.ts
import { listRepoTree } from '@huggingface/hub';

export interface ManifestEntry {
  date: string;
  slug: string;
  path: string;
  url: string;
  sha256?: string;
  size: number;
}

export interface Manifest {
  generatedAt: string;
  repo: string;
  date: string;
  slug: string;
  entries: ManifestEntry[];
}

const CDN_ROOT = 'https://huggingface.co/datasets';

export async function buildManifest(
  repo: string,
  date: string,
  slug: string,
  options?: { token?: string }
): Promise<Manifest> {
  const prefix = `batches/mirror-merged/${date}/${slug}`;
  
  const tree = await listRepoTree({
    repo,
    path: prefix,
    recursive: true,
    ...(options?.token && { token: options.token })
  });

  const entries: ManifestEntry[] = (tree.entries || [])
    .filter((e) => e.type === 'file' && /\.(parquet|jsonl|csv)$/i.test(e.path))
    .map((e) => ({
      date,
      slug,
      path: e.path,
      url: `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(e.path)}`,
      sha256: e.lfs?.oid,
      size: e.size || 0
    }));

  return {
    generatedAt: new Date().toISOString(),
    repo,
    date,
    slug,
    entries
  };
}
```

### 3. Runtime CDN Service (`src/frontend/services/cdn.ts`)
*Zero HF API calls at runtime - pure CDN fetches*

```ts
// @/frontend/services/cdn.ts
import type { Manifest } from '../../data/manifest';

const CDN_ROOT = 'https://huggingface.co/datasets';

export class CDNService {
  private manifestCache: Map<string, Manifest> = new Map();

  async loadManifest(date: string, slug: string, repo: string): Promise<Manifest> {
    const key = `${date}/${slug}`;
    
    if (this.manifestCache.has(key)) {
      return this.manifestCache.get(key)!;
    }

    // Fetch pre-built manifest via CDN (no auth)
    const manifestUrl = `${CDN_ROOT}/${repo}/resolve/main/manifests/${date}/${slug}.json`;
    const res = await fetch(manifestUrl, {
      headers: { Accept: 'application/json' },
      cache: 'force-cache' // Leverage browser cache
    });

    if (!res.ok) {
      throw new Error(`Manifest fetch failed: ${res.status} ${res.statusText}`);
    }

    const manifest = await res.json();
    this.manifestCache.set(key, manifest);
    return manifest;
  }

  async fetchParquetArrayBuffer(
    date: string,
    slug: string,
    path: string,
    repo: string
  ): Promise<ArrayBuffer> {
    const url = `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(path)}`;
    const res = await fetch(url, {
      method: 'GET',
      headers: { 
        Accept: 'application/octet-stream',
        // No Authorization - pure CDN
      },
      cache: 'default'
    });

    if (!res.ok) {
      throw new Error(`CDN fetch failed: ${res.status} ${res.statusText} for ${path}`);
    }
    return res.arrayBuffer();
  }

  getCDNUrl(path: string, repo: string): string {
    return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(path)}`;
  }
}
```

### 4. Lightning Studio Reuse (`src/orchestration/studio.ts`)
*Prevents duplicate Studio creation and idle-stop waste*

```ts
// @/orchestration/studio.ts
import type { Studio } from 'lightning-sdk';

export class StudioManager {
  private static instance: StudioManager;
  private activeStudios: Map<string, Studio> = new Map();

  static getInstance(): StudioManager {
    if (!StudioManager.instance) {
      StudioManager.instance = new StudioManager();
    }
    return StudioManager.instance;
  }

  async getOrCreateStudio(
    Teamspace: any,
    studioName: string,
    machine: any,
    idleTimeoutMinutes: number = 30
  ): Promise<Studio> {
    // Check existing running studios first
    const studios = await Teamspace.studios();
    const running = studios.find(
      (s: Studio) => s.name === studioName && s.status === 'Running'
    );

    if (running) {
      // Reset idle timer on reuse
      await this.resetIdleTimeout(running, idleTimeoutMinutes);
      this.activeStudios.set(studioName, running);
      return running;
    }

    // Create new studio with proper lifecycle
    const Studio = (await import('lightning-sdk')).Studio;
    const studio = await Studio.create({
      name: studioName,
      machine,
      idleTimeoutMinutes,
      createOk: true
    });

    this.activeStudios.set(studioName, studio);
    return studio;
  }

  private async resetIdleTimeout(studio: Studio, minutes: number): Promise<void> {
    // Implementation depends on Lightning SDK capabilities
    // Typically involves PATCH /studio/{id} with updated idleTimeout
    if (studio.updateSettings) {
      await studio.updateSettings({ idleTimeoutMinutes: minutes });
    }
  }

  async stopStudio(studioName: string): Promise<void> {
    const studio = this.activeStudios.get(studioName);
    if (studio && studio.stop) {
      await studio.stop();
      this.activeStudios.delete(studioName);
    }
  }
}
```

### 5. Manifest Utilities (`src/frontend/utils/manifest.ts`)
*Lightweight runtime helpers for deterministic key generation*

```ts
// @/frontend/utils/manifest.ts
export interface ManifestEntry {
  date: string;
  slug: string;
  path: string;
  url: string;
  sha256?: string;
  size: number;
}

export function manifestKey(date: string, slug: string): string {
  return `${date}/${slug}`;
}

export function parseManifestKey(key: string): { date: string; slug: string } {
  const [date, ...slugParts] = key.split('/');
  return { date, slug: slugParts.join('/') };
}

// Content-addressed slug generator (deterministic)
export function generateSlug(content: Buffer | string): string {
  const crypto = require('crypto');
  const hash = crypto.createHash('sha256');
  hash.update(content);
  return hash.digest('hex').substring(0, 12);
}
```

### 6. Verification Protocol

**Step 1: Build Manifest (Mac orchestration)**
```bash
node -e "
import { buildManifest } from './src/data/manifest.js';
const manifest = await buildManifest('your-org/dataset', '2026-05-03', 'abc123');
await fs.writeFile('manifests/2026-05-03/abc123.json', JSON.stringify(manifest, null, 2));
"
```

**Step 2: Runtime CDN Test (Browser DevTools)**
```javascript
import { CDNService } from './src/frontend/services/cdn.js';

const cdn = new CDNService();
const manifest = await cdn.loadManifest('
