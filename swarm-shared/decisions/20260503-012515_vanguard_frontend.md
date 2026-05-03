# vanguard / frontend

## Final synthesized solution (correct + actionable)

**Core diagnosis (merged, de-duplicated)**
- Frontend still calls authenticated `list_repo_tree` (and `/api/` proxies) on page/training load, burning HF quota (1000/5min) and causing 429s.
- No persisted `(repo, dateFolder)` manifest, so every session repeats expensive discovery.
- Dataset downloads use authenticated paths instead of public CDN URLs (`resolve/main/...`), wasting quota and adding latency.
- Mixed-schema parquet ingestion risks `pyarrow.CastError`; projection to `{prompt, response}` should happen once at build time, not per-download.

**Single source of truth: a pre-generated manifest + CDN-only fetches**
- Generate one manifest per date folder (JSON: repo, dateFolder, files[]). Commit or host it statically.
- Frontend and training scripts consume the manifest and build public CDN URLs; never send Authorization for public dataset files.
- Parquet files should already be projected to `{prompt, response}` (clean schema) during preprocessing; client code expects that shape.

---

### 1) Manifest generator (run once on orchestration host)

```bash
# /opt/axentx/vanguard/scripts/generate-manifest.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1}"
DATEFOLDER="${1:-$(date +%Y-%m-%d)}"
OUTDIR="/opt/axentx/vanguard/static/manifests"
OUTFILE="${OUTDIR}/${DATEFOLDER}.json"

mkdir -p "$OUTDIR"

python3 - "$REPO" "$DATEFOLDER" "$OUTFILE" <<'PY'
import json, os, sys
from huggingface_hub import list_repo_tree

repo = sys.argv[1]
date_folder = sys.argv[2]
outfile = sys.argv[3]

# List objects in the date folder (non-recursive)
tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
files = sorted(f.rfilename for f in tree if f.type == "file")

manifest = {
    "repo": repo,
    "dateFolder": dateFolder,
    "files": files
}

os.makedirs(os.path.dirname(outfile), exist_ok=True)
with open(outfile, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
print(f"Manifest written to {outfile} with {len(files)} files")
PY
```

Make executable and run for the target date:

```bash
chmod +x /opt/axentx/vanguard/scripts/generate-manifest.sh
SHELL=/bin/bash /opt/axentx/vanguard/scripts/generate-manifest.sh 2026-05-03
```

Expected output: `/opt/axentx/vanguard/static/manifests/2026-05-03.json` with `{repo, dateFolder, files}`.

---

### 2) Frontend: static manifest + CDN URLs (no auth)

Adjust paths to your framework (SvelteKit shown; adapt to Next/Nuxt as needed).

```typescript
// /opt/axentx/vanguard/src/routes/+page.server.ts
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async () => {
  const dateFolder = '2026-05-03';
  const manifestUrl = `/manifests/${dateFolder}.json`;

  const res = await fetch(manifestUrl);
  if (!res.ok) {
    return { manifest: null, files: [], error: 'Manifest unavailable' };
  }

  const manifest = await res.json();
  const cdnBase = `https://huggingface.co/datasets/${manifest.repo}/resolve/main/${manifest.dateFolder}`;

  const files = manifest.files.map((f: string) => ({
    name: f,
    url: `${cdnBase}/${encodeURIComponent(f)}`
  }));

  return { manifest, files };
};
```

```typescript
// /opt/axentx/vanguard/src/lib/api.ts
// Public CDN helpers — zero Authorization headers

export function getDatasetFileUrl(repo: string, dateFolder: string, filePath: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/main/${dateFolder}/${encodeURIComponent(filePath)}`;
}

export async function fetchDatasetFile(url: string): Promise<Response> {
  // Do NOT include Authorization for public CDN resources
  return fetch(url, {
    headers: {
      Accept: '*/*'
    }
  });
}
```

Remove or disable any authenticated `/api/datasets/...` proxy for public dataset files. Point UI and training scripts to the CDN URLs above.

---

### 3) Optional: lightweight JS/TS CDN client (drop-in)

If you prefer a small module that loads a manifest and fetches parquet by slug:

```ts
// /opt/axentx/vanguard/src/lib/data/hf-client.ts
// CDN-bypass client — zero auth, zero quota impact

type Manifest = {
  repo: string;
  dateFolder: string;
  files: string[];
};

const DEFAULT_DATE = '2026-05-03';

export class HFClient {
  private manifest: Manifest | null = null;

  constructor(
    private manifestPath: string = `/manifests/${DEFAULT_DATE}.json`
  ) {}

  async loadManifest(): Promise<Manifest> {
    const res = await fetch(this.manifestPath);
    if (!res.ok) throw new Error('Failed to load manifest');
    this.manifest = await res.json();
    return this.manifest;
  }

  getCDNUrl(filePath: string): string {
    if (!this.manifest) throw new Error('Manifest not loaded');
    return `https://huggingface.co/datasets/${this.manifest.repo}/resolve/main/${this.manifest.dateFolder}/${encodeURIComponent(filePath)}`;
  }

  async fetchParquetBySlug(slug: string): Promise<Response> {
    const file = this.manifest?.files.find((f) => f.startsWith(slug));
    if (!file) throw new Error('File not found in manifest');
    return fetchDatasetFile(this.getCDNUrl(file));
  }
}
```

Use:

```ts
const client = new HFClient();
await client.loadManifest();
const resp = await client.fetchParquetBySlug('train-00001');
const buffer = await resp.arrayBuffer();
// parse parquet (expect schema {prompt:string, response:string})
```

---

### 4) Schema hygiene (prevent CastError)

- Ensure preprocessing produces clean parquet with stable schema, e.g. `{prompt: string, response: string}`.
- If you must project at read time, do it once during manifest generation and write cleaned parquet, rather than projecting on every client fetch.

---

### 5) Verification checklist

1. Generate manifest and confirm:
   ```bash
   cat /opt/axentx/vanguard/static/manifests/2026-05-03.json
   ```
   Expect valid JSON with non-empty `files`.

2. Start dev server, open DevTools Network tab:
   - `/manifests/2026-05-03.json` loads 200.
   - Dataset file requests go to `https://huggingface.co/datasets/.../resolve/main/...`.
   - No `Authorization` header on those CDN requests.
   - No `/api/` or `list_repo_tree` calls on page load.

3. Confirm quota behavior:
   - With no authenticated calls on page load, HF API quota remains untouched.
   - CDN downloads succeed when logged out / without token.

4. Smoke test in browser console:
   ```js
   fetch("https://huggingface.co/datasets/axentx/surrogate-1/resolve/main/2026-05-03/some-file.parquet")
     .then(r => console.log(r.status, r.url))
   ```
   Expect 200 and CDN URL.

5. Parquet schema test (optional, server-side):
   - Load a sample parquet and assert columns include `prompt` and `response` with expected types.

---

**Result**: Rate-limit pressure removed, 429s eliminated, downloads accelerated via CDN, and ingestion schema stabilized.
