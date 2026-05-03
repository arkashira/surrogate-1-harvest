# vanguard / quality

## 1. Diagnosis

- No build-time deterministic asset manifest → frontend cannot resolve hashed bundles or CDN paths without runtime discovery, risking 429s and cache misses.
- Missing mount point / entrypoint boundary between orchestration host and browser runtime → unclear where CDN assets are injected and how integrity is enforced.
- No content-hash-based filenames for build outputs → cache-busting relies on ad-hoc query strings or timestamps instead of immutable content hashes.
- No Subresource Integrity (SRI) generation or consumption → browser loads unverified bundles from CDN, weakening supply-chain guarantees.
- No reproducible build script that pins file list and emits a minimal `index.html` → each deploy can diverge and require runtime HF API calls.

## 2. Proposed change

Add a small, deterministic build step that:
- Scans `dist/` (or build output) once at build time.
- Produces `public/manifest.json` mapping logical names to `{ file, hash, integrity }`.
- Emits `public/index.html` that references only CDN URLs with `integrity` attributes.
- Keeps the frontend entirely static (zero runtime HF API calls).

Scope:
- Create `/opt/axentx/vanguard/scripts/build-manifest.js`
- Create `/opt/axentx/vanguard/public/index.html` (template consumed by script)
- Optional: add `package.json` script `"build:static"` if Node tooling is present; otherwise provide a POSIX-compliant shell script.

## 3. Implementation

Create the build script:

```bash
# /opt/axentx/vanguard/scripts/build-manifest.sh
#!/usr/bin/env bash
set -euo pipefail

# Deterministic build-time manifest for CDN-first frontend
# Usage: ./build-manifest.sh [dist_dir] [out_dir]
# Emits: {out_dir}/manifest.json and {out_dir}/index.html

DIST_DIR="${1:-dist}"
OUT_DIR="${2:-public}"
CDN_ROOT="https://huggingface.co/datasets/axentx/vanguard/resolve/main"

mkdir -p "$OUT_DIR"

# Build manifest: { "app.js": { "file": "...", "sha384": "...", "integrity": "sha384-..." }, ... }
echo "{" > "$OUT_DIR/manifest.json"
first=true
while IFS= read -r -d '' file; do
  rel="${file#$DIST_DIR/}"
  # Skip if not a regular file
  [ -f "$file" ] || continue
  hash=$(openssl dgst -sha384 -binary "$file" | base64 -w0)
  integrity="sha384-$hash"
  url="$CDN_ROOT/$rel"

  $first || printf ",\n" >> "$OUT_DIR/manifest.json"
  first=false

  printf '  "%s": {\n    "file": "%s",\n    "sha384": "%s",\n    "integrity": "%s",\n    "url": "%s"\n  }' \
    "$rel" "$rel" "$hash" "$integrity" "$url" >> "$OUT_DIR/manifest.json"
done < <(find "$DIST_DIR" -type f -print0 | sort -z)

echo "" >> "$OUT_DIR/manifest.json"
echo "}" >> "$OUT_DIR/manifest.json"

# Emit minimal index.html that uses the manifest
cat > "$OUT_DIR/index.html" <<'EOF'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <style>
    /* Minimal critical CSS to avoid FOUC; keep this tiny and cacheable */
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0}
  </style>
</head>
<body>
  <div id="app"></div>
  <!-- Assets injected by build-manifest.sh; no runtime HF API calls -->
  <script>
    // Lightweight runtime loader that consumes manifest.json produced at build time.
    // If manifest is missing, fail fast and visibly.
    (function () {
      var manifestUrl = '/manifest.json';
      fetch(manifestUrl, {cache: 'no-cache'}).then(function (r) {
        if (!r.ok) throw new Error('Manifest fetch failed: ' + r.status);
        return r.json();
      }).then(function (manifest) {
        var app = manifest['app.js'] || manifest['main.js'] || Object.values(manifest)[0];
        if (!app) throw new Error('No app script found in manifest');
        var s = document.createElement('script');
        s.src = app.url;
        s.integrity = app.integrity;
        s.crossOrigin = 'anonymous';
        s.onerror = function () {
          document.body.innerHTML = '<p style="color:#ef4444;padding:1rem">Failed to load application bundle (SRI/network).</p>';
        };
        document.head.appendChild(s);
      }).catch(function (err) {
        document.body.innerHTML = '<p style="color:#ef4444;padding:1rem">' + String(err) + '</p>';
        console.error(err);
      });
    })();
  </script>
</body>
</html>
EOF

echo "Manifest and index.html written to $OUT_DIR/"
```

Make it executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/build-manifest.sh
```

If Node is preferred (optional), add `/opt/axentx/vanguard/scripts/build-manifest.js`:

```js
#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DIST_DIR = process.argv[2] || 'dist';
const OUT_DIR = process.argv[3] || 'public';
const CDN_ROOT = 'https://huggingface.co/datasets/axentx/vanguard/resolve/main';

fs.mkdirSync(OUT_DIR, { recursive: true });

function sha384Base64(file) {
  return crypto.createHash('sha384').update(fs.readFileSync(file)).digest('base64');
}

const entries = fs.readdirSync(DIST_DIR, { withFileTypes: true })
  .filter(e => e.isFile())
  .map(e => e.name)
  .sort();

const manifest = {};
for (const name of entries) {
  const file = path.join(DIST_DIR, name);
  const hash = sha384Base64(file);
  manifest[name] = {
    file: name,
    sha384: hash,
    integrity: `sha384-${hash}`,
    url: `${CDN_ROOT}/${name}`
  };
}

fs.writeFileSync(path.join(OUT_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2));

const indexHtml = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <style>body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0}</style>
</head>
<body>
  <div id="app"></div>
  <script>
    (function () {
      var manifestUrl = '/manifest.json';
      fetch(manifestUrl, {cache: 'no-cache'}).then(function (r) {
        if (!r.ok) throw new Error('Manifest fetch failed: ' + r.status);
        return r.json();
      }).then(function (manifest) {
        var app = manifest['app.js'] || manifest['main.js'] || Object.values(manifest)[0];
        if (!app) throw new Error('No app script found in manifest');
        var s = document.createElement('script');
        s.src = app.url;
        s.integrity = app.integrity;
        s.crossOrigin = 'anonymous';
        s.onerror = function () {
          document.body.innerHTML = '<p style="color:#ef4444;padding:1rem">Failed to load application bundle (SRI/network).</p>';
        };
        document.head.appendChild(s);
      }).catch(function (err) {
        document.body.innerHTML = '<p style="color:#ef4444;padding:1rem">' + String(err) + '</p>';

