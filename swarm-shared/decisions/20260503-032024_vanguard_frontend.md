# vanguard / frontend

## 1. Diagnosis
- No CDN-first manifest exists; ingestion/training scripts likely still call `list_repo_tree`/`load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training jobs cannot pin exact data versions.
- Frontend has no deterministic data-selection UI or manifest viewer, so operators can’t verify what data a training run will consume before launch.
- No lightweight preview of manifest contents (counts, date ranges, repo sources) to catch schema drift early.
- No Makefile/CLI target to generate or validate manifests locally before pushing to Lightning.

## 2. Proposed change
Add a frontend-centric manifest generator and viewer under `frontend/` that produces a CDN-first JSONL manifest and a minimal React component to browse it. Scope:
- `frontend/scripts/generate-manifest.js` — reads a local `batches/mirror-merged/{date}/**/*.parquet` tree (or accepts a JSON file list) and emits `manifests/{date}-manifest.jsonl` with `{date, slug, cdn_url, sha256, size}` per file.
- `frontend/components/ManifestViewer.jsx` — tiny table with search/filter and a “Copy training args” button that emits a Lightning-safe `--file-list` JSON path.
- `Makefile` targets: `make manifest DATE=2026-04-29` and `make check-manifest DATE=2026-04-29`.

## 3. Implementation

### frontend/scripts/generate-manifest.js
```js
#!/usr/bin/env node
// Usage: node generate-manifest.js --date 2026-04-29 --root ./batches --out ./manifests
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function sha256File(filePath) {
  const buf = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function buildManifest(date, root, outDir) {
  const dayDir = path.join(root, date);
  if (!fs.existsSync(dayDir)) {
    console.error(`Date folder not found: ${dayDir}`);
    process.exit(1);
  }

  const repo = 'datasets/axentx/vanguard-mirror'; // configurable via env
  const outFile = path.join(outDir, `${date}-manifest.jsonl`);
  const lines = [];

  function walk(dir) {
    for (const entry of fs.readdirSync(dir)) {
      const full = path.join(dir, entry);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) {
        walk(full);
      } else if (entry.endsWith('.parquet')) {
        const rel = path.relative(root, full);
        const slug = rel.replace(/\.parquet$/, '').replace(/\\/g, '/');
        const cdnUrl = `https://huggingface.co/${repo}/resolve/main/${rel}`;
        lines.push(JSON.stringify({
          date,
          slug,
          cdn_url: cdnUrl,
          sha256: sha256File(full),
          size: stat.size,
          local_path: full
        }));
      }
    }
  }

  walk(dayDir);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(outFile, lines.join('\n') + '\n');
  console.log(`Wrote ${lines.length} entries to ${outFile}`);
  return outFile;
}

// CLI
const args = process.argv.slice(2);
const opts = { date: null, root: './batches', out: './manifests' };
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--date' && args[i + 1]) opts.date = args[++i];
  if (args[i] === '--root' && args[i + 1]) opts.root = args[++i];
  if (args[i] === '--out' && args[i + 1]) opts.out = args[++i];
}
if (!opts.date) {
  console.error('Missing --date (e.g., 2026-04-29)');
  process.exit(1);
}
buildManifest(opts.date, opts.root, opts.out);
```

### frontend/components/ManifestViewer.jsx
```jsx
import React, { useEffect, useState } from 'react';

export default function ManifestViewer({ manifestPath }) {
  const [rows, setRows] = useState([]);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    fetch(manifestPath)
      .then(r => r.text())
      .then(text => text.trim().split('\n').filter(Boolean).map(l => JSON.parse(l)))
      .then(setRows)
      .catch(console.error);
  }, [manifestPath]);

  const filtered = rows.filter(r => r.slug.toLowerCase().includes(filter.toLowerCase()));
  const totalSize = filtered.reduce((s, r) => s + (r.size || 0), 0);

  const copyTrainingArgs = () => {
    const fileList = filtered.map(r => r.cdn_url);
    navigator.clipboard.writeText(JSON.stringify(fileList, null, 2));
    alert('CDN file list copied to clipboard (use in Lightning train.py)');
  };

  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', padding: 16 }}>
      <h3>Manifest: {manifestPath}</h3>
      <p>{rows.length} files • {filtered.length} shown</p>
      <input
        placeholder="Filter by slug"
        value={filter}
        onChange={e => setFilter(e.target.value)}
        style={{ padding: 6, width: '100%', marginBottom: 12 }}
      />
      <button onClick={copyTrainingArgs} style={{ marginBottom: 12, padding: '6px 12px' }}>
        Copy training args (CDN file list)
      </button>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid #ddd' }}>
            <th style={{ textAlign: 'left', padding: 6 }}>slug</th>
            <th style={{ textAlign: 'right', padding: 6 }}>size</th>
            <th style={{ textAlign: 'left', padding: 6 }}>sha256</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map((r, i) => (
            <tr key={i} style={{ borderBottom: '1px solid #eee' }}>
              <td style={{ padding: 6, fontFamily: 'monospace', fontSize: 12 }}>{r.slug}</td>
              <td style={{ padding: 6, textAlign: 'right' }}>{(r.size / 1024).toFixed(1)} KB</td>
              <td style={{ padding: 6, fontFamily: 'monospace', fontSize: 10 }}>{r.sha256.slice(0, 12)}…</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

### Makefile additions
```make
DATE ?= 2026-04-29
MANIFEST_OUT := manifests

.PHONY: manifest check-manifest

manifest:
	@node frontend/scripts/generate-manifest.js --date $(DATE) --root batches --out $(MANIFEST_OUT)

check-manifest: manifest
	@echo "Checking $(MANIFEST_OUT)/$(DATE)-manifest.jsonl"
	@wc -l $(MANIFEST_OUT)/$(DATE)-manifest.jsonl
	@head -n 3 $(MANIFEST_OUT)/$(DATE)-manifest.jsonl
```

## 4. Verification
1. Place sample parquet files under `batches/mirror-merged/2026-04-29/` (or any date).
2. Run `make manifest DATE=2026-04-29` — should print entry count and create `manifests/2026-04-29-manifest.jsonl`.
3. Inspect a few lines: each should contain `
