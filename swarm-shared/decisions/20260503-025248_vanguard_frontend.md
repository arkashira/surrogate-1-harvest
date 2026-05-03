# vanguard / frontend

## 1. Diagnosis

- No deterministic asset manifest at build time → frontend cannot reliably reference hashed bundles/CDN assets; cache-busting and integrity checks are ad-hoc.
- Missing mount point and entrypoint boundary → no clear separation between orchestration host and browser runtime; mounting is fragile and environment-dependent.
- No content-hashed filenames for static assets → long-term caching is impossible; any change busts all caches or none.
- Runtime asset resolution relies on fragile path assumptions instead of generated manifest → breaks during CDN-first or proxy deployments.
- No minimal, reproducible dev/build script to produce the manifest and entry HTML → local iteration is inconsistent and cannot be automated.

## 2. Proposed change

Create a minimal, deterministic frontend build pipeline that:
- Generates `dist/manifest.json` mapping logical names to content-hashed filenames (JS/CSS).
- Produces a single `dist/index.html` with correct `<script>`/`<link>` references and a stable mount point (`<div id="root">`).
- Uses only Node (no heavy bundler config) so it can run locally and in CI in <30s.

File scope:
- Add `/opt/axentx/vanguard/package.json` (if absent) with build/dev scripts.
- Add `/opt/axentx/vanguard/build.js` — manifest + HTML generator.
- Add `/opt/axentx/vanguard/src/entry.js` — app entry (minimal).
- Add `/opt/axentx/vanguard/src/styles.css` — minimal styles.
- Output to `/opt/axentx/vanguard/dist/`.

## 3. Implementation

```bash
cd /opt/axentx/vanguard
```

### package.json

```json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "node build.js",
    "dev": "node build.js --watch"
  },
  "devDependencies": {
    "esbuild": "^0.21.0"
  }
}
```

### build.js

```js
// build.js
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { build } from 'esbuild';

const root = process.cwd();
const srcDir = path.join(root, 'src');
const distDir = path.join(root, 'dist');
const manifestPath = path.join(distDir, 'manifest.json');

function hash(content) {
  return crypto.createHash('sha256').update(content).digest('hex').slice(0, 12);
}

async function buildAll() {
  if (!fs.existsSync(distDir)) fs.mkdirSync(distDir, { recursive: true });

  // Build JS
  const jsResult = await build({
    entryPoints: [path.join(srcDir, 'entry.js')],
    bundle: true,
    minify: true,
    write: false,
    target: 'es2020'
  });
  const jsHash = hash(jsResult.outputFiles[0].contents);
  const jsName = `entry.${jsHash}.js`;
  fs.writeFileSync(path.join(distDir, jsName), jsResult.outputFiles[0].contents);

  // Build CSS
  const cssContent = fs.readFileSync(path.join(srcDir, 'styles.css'), 'utf8');
  const cssHash = hash(cssContent);
  const cssName = `styles.${cssHash}.css`;
  fs.writeFileSync(path.join(distDir, cssName), cssContent);

  // Manifest
  const manifest = {
    files: {
      main: jsName,
      styles: cssName
    },
    mountPoint: 'root',
    generatedAt: new Date().toISOString()
  };
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

  // index.html
  const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <link rel="stylesheet" href="./${cssName}" />
</head>
<body>
  <div id="root"></div>
  <script type="module" src="./${jsName}"></script>
</body>
</html>`;
  fs.writeFileSync(path.join(distDir, 'index.html'), html);

  console.log('Built manifest + assets -> dist/');
  console.log(manifest);
}

// Simple watch mode for dev
if (process.argv.includes('--watch')) {
  buildAll();
  fs.watch(srcDir, { recursive: true }, () => buildAll());
} else {
  buildAll().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

### src/entry.js

```js
// Minimal entry — mount point boundary
const root = document.getElementById('root');

if (!root) {
  throw new Error('Mount point #root not found in index.html');
}

root.innerHTML = `
  <main style="font-family: system-ui, sans-serif; padding: 1rem;">
    <h1>Vanguard</h1>
    <p id="status">Initializing…</p>
  </main>
`;

// Example runtime behavior (replace with real app bootstrap)
const status = document.getElementById('status');
status.textContent = 'Ready — manifest-driven build active.';
```

### src/styles.css

```css
/* Minimal styles */
:root {
  --bg: #f7f7fb;
  --fg: #111827;
  --muted: #6b7280;
}

html, body {
  height: 100%;
  margin: 0;
  background: var(--bg);
  color: var(--fg);
}

#root {
  min-height: 100%;
}
```

Install and run:

```bash
npm install
npm run build
```

## 4. Verification

1. Run `npm run build`. Confirm:
   - `dist/manifest.json` exists and contains `files.main` and `files.styles` with content-hashed filenames.
   - `dist/index.html` references those exact filenames and contains `<div id="root"></div>`.
   - No references to external CDNs or runtime HF API calls in the generated HTML/JS.

2. Serve `dist/` with any static server and open in browser:
   ```bash
   npx serve dist
   ```
   - Page renders “Vanguard” and status text “Ready — manifest-driven build active.”
   - Network tab shows only the hashed JS/CSS assets (no 404s).

3. Repeat build twice with small changes to `src/entry.js` or `src/styles.css`:
   - Confirm content hashes change and `index.html` updates accordingly (deterministic, cache-friendly).
   - Confirm `manifest.json` updates with new filenames.

4. (Optional) Run `npm run dev`, edit a source file, and confirm the watcher rebuilds manifest + assets automatically.
