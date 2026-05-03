# vanguard / frontend

## Final synthesis (best parts, contradictions resolved for correctness + actionability)

**What we must fix**
- HF API 429s and non-reproducible runs are caused by runtime `list_repo_tree`/`load_dataset` calls from frontend-triggered flows.
- Missing deterministic, content-addressed snapshot keyed by date/slug causes re-enumeration, re-ingest, and commit/rate-limit risk.
- No embedded, CDN-first file list for Lightning Studio/training jobs forces Mac orchestration to re-list on every run.
- Schema drift at ingest (extra columns in `enriched/`) breaks downstream training expectations.
- No lightweight verification before training starts and no reproducible training launcher.

**Chosen approach (merged + corrected)**
- Create a **CDN-first manifest generator + validator** (TypeScript/Bun) that produces a stable JSON snapshot per date folder with `{slug, cdnUrl, sha256, size}`.
- Add a **schema-projection step at ingest** so `enriched/` always contains strict `{prompt,response}` parquet files with filename attribution (date/slug) and no extra columns.
- Add a **ManifestStatus React component** and a **reproducible `train.py` launcher** that consumes the manifest and trains only on the pinned snapshot.
- Use **HEAD checks (CDN)** for validation (does not consume HF API quota).
- Provide **scripts + CI/local entrypoints** to generate and commit manifests once per date folder, and fail fast if the manifest is missing or invalid before training.

**Resolved contradictions**
- Candidate 2’s “local fallback/cache layer for file lists” is implemented as the committed JSON manifest + optional local cache read in training (not runtime HF listing). This is concrete and sufficient.
- Candidate 2’s “versioned train.py + manifest” is included as a reproducible launcher.
- Candidate 1’s runtime sha256 computation is made **optional** (skip by default; enable via flag) to avoid heavy downloads during manifest generation. Validation uses lightweight HEAD checks.
- Both schema enforcement and manifest generation are required; we do not pick one over the other.

---

## Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/frontend/src/manifest
mkdir -p /opt/axentx/vanguard/frontend/src/components
mkdir -p /opt/axentx/vanguard/training
mkdir -p /opt/axentx/vanguard/scripts
```

### `/opt/axentx/vanguard/frontend/src/manifest/createManifest.ts`
```ts
import { listRepoTree } from '../lib/hfApi';

export interface ManifestEntry {
  slug: string;          // e.g. "2026-04-29/sample-abc123.parquet"
  cdnUrl: string;        // https://huggingface.co/datasets/{repo}/resolve/main/{slug}
  sha256?: string;
  size: number;
}

export interface Manifest {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  files: ManifestEntry[];
}

/**
 * Generate a CDN-first manifest for one dateFolder.
 * Run from Mac orchestration (once per date) and commit the JSON.
 *
 * Note: sha256 is optional and disabled by default (pass computeSha256=true to enable).
 */
export async function createManifest(
  repo: string,
  dateFolder: string,
  outPath: string,
  computeSha256 = false
): Promise<Manifest> {
  // Single API call: non-recursive tree for the date folder
  const tree = await listRepoTree({ repo, path: dateFolder, recursive: false });

  const files: ManifestEntry[] = [];
  for (const item of tree) {
    if (item.type !== 'file') continue;
    const slug = `${dateFolder}/${item.path}`;
    const cdnUrl = `https://huggingface.co/datasets/${repo}/resolve/main/${slug}`;
    files.push({
      slug,
      cdnUrl,
      size: item.size || 0,
    });
  }

  // Optional: compute sha256 by downloading (slow). Only enable when explicitly requested.
  if (computeSha256) {
    const { sha256File, downloadTemp } = await import('../lib/crypto');
    for (const f of files) {
      try {
        f.sha256 = await sha256File(await downloadTemp(f.cdnUrl));
      } catch {
        f.sha256 = undefined;
      }
    }
  }

  const manifest: Manifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };

  await Bun.write(outPath, JSON.stringify(manifest, null, 2));
  return manifest;
}

// CLI entrypoint
if (import.meta.main) {
  const args = process.argv.slice(2);
  const repo = args[0];
  const dateFolder = args[1];
  const outPath = args[2];
  const computeSha256 = args.includes('--sha256');

  if (!repo || !dateFolder || !outPath) {
    console.error('Usage: createManifest <repo> <dateFolder> <outPath> [--sha256]');
    process.exit(1);
  }
  await createManifest(repo, dateFolder, outPath, computeSha256);
  console.log(`Manifest written to ${outPath}`);
}
```

### `/opt/axentx/vanguard/frontend/src/manifest/validateManifest.ts`
```ts
/**
 * HEAD-check every CDN URL in the manifest.
 * CDN requests do not count against HF API rate limits.
 */
export async function validateManifest(manifestPath: string): Promise<Array<{ slug: string; ok: boolean; status?: number; error?: string }>> {
  const manifest = JSON.parse(await Bun.file(manifestPath).text()) as { files: Array<{ slug: string; cdnUrl: string }> };
  const results: Array<{ slug: string; ok: boolean; status?: number; error?: string }> = [];

  // Limit concurrency to avoid local port exhaustion
  const limit = 20;
  const queue = [...manifest.files];
  const workers = Array.from({ length: limit }, async () => {
    while (queue.length) {
      const item = queue.pop()!;
      try {
        const res = await fetch(item.cdnUrl, { method: 'HEAD' });
        results.push({ slug: item.slug, ok: res.ok, status: res.status });
      } catch (err) {
        results.push({ slug: item.slug, ok: false, error: String(err) });
      }
    }
  });
  await Promise.all(workers);
  return results;
}

if (import.meta.main) {
  const [manifestPath] = process.argv.slice(2);
  if (!manifestPath) {
    console.error('Usage: validateManifest <manifestPath>');
    process.exit(1);
  }
  const results = await validateManifest(manifestPath);
  const failed = results.filter((r) => !r.ok);
  console.log(`Checked ${results.length}, OK: ${results.length - failed.length}, Failed: ${failed.length}`);
  if (failed.length) {
    console.table(failed.slice(0, 20));
    process.exit(1);
  }
}
```

### `/opt/axentx/vanguard/frontend/src/components/ManifestStatus.tsx`
```tsx
import { useState, useEffect } from 'react';

interface ManifestMeta {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  filesCount: number;
}

export function ManifestStatus({ manifestPath }: { manifestPath: string }) {
  const [meta, setMeta] = useState<ManifestMeta | null>(null);
  const [health, setHealth] = useState<'loading' | 'ok' | 'degraded' | 'missing'>('loading');

  useEffect(() => {
    fetch(manifestPath)
      .then((r) => {
        if (!r.ok) throw new Error('Manifest missing');
        return r.json();
      })
      .then((m) => {
        setMeta({
          repo: m.repo,
          dateFolder: m.dateFolder,
          generatedAt: m.generatedAt,
          filesCount: m.files?.length || 0,
        });
        setHealth('ok');
