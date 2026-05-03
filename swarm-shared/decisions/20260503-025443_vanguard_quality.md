# vanguard / quality

## Final Synthesis (Best Parts + Correctness + Actionability)

**Diagnosis (merged, de-duplicated, prioritized)**
- No deterministic asset manifest at build time → frontend cannot reliably reference hashed/CDN bundles; cache-busting and integrity checks are ad-hoc.
- Missing mount-point/entrypoint boundary → no clear separation between orchestration host and browser runtime; hydration/mounting are implicit and fragile.
- No build-time validation of the asset graph → broken/missing chunks can reach production and cause runtime 404s or integrity failures.
- No reproducible build script → CI/CD and local dev can diverge.
- No enforced Subresource Integrity (SRI) or CSP-friendly injection → CDN assets load without verification, increasing supply-chain/cache-poisoning risk.

---

## Proposed Change (single, focused deliverable)

Add a lightweight, deterministic build pipeline and strict runtime boundary:

- `vanguard/frontend/build.sh` — reproducible build entrypoint (set -euo pipefail, install/build/generate).
- `vanguard/frontend/build.py` — generates deterministic `dist/manifest.json` mapping logical names to `{file,hash,integrity,size?}` with SRI.
- `vanguard/frontend/templates/index.html` — server-side template (or static fallback) that consumes manifest and injects `<link>`/`<script>` with `integrity` and `crossorigin`.
- `vanguard/frontend/entrypoint.js` — enforces single mount point (`#app`) and fails fast if missing; exposes safe bootstrap hook.

Scope: ~120–150 lines total. Focused on build-time quality and runtime safety.

---

## Implementation (merged + hardened)

```bash
# vanguard/frontend/build.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Optional: ensure dist is clean for reproducibility (CI-friendly)
rm -rf dist
mkdir -p dist

# Build assets (replace with your bundler)
npm run build 2>&1 | tail -10

# Generate deterministic manifest
python3 build.py

echo "Build complete. Manifest written to dist/manifest.json"
```

```python
# vanguard/frontend/build.py
#!/usr/bin/env python3
"""
Deterministic build manifest generator.
Produces dist/manifest.json:
{
  "main.js": {
    "file": "assets/main.3a7f2c9b.js",
    "integrity": "sha384-...",
    "size": 12345
  },
  "main.css": { ... }
}
"""

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

DIST_DIR = Path(__file__).parent / "dist"
MANIFEST_PATH = DIST_DIR / "manifest.json"
INCLUDE_EXTS = {".js", ".css"}
HASH_ALGO = "sha384"  # SRI-compatible

def hash_file(path: Path) -> str:
    h = hashlib.new(HASH_ALGO.replace("sha", "sha"))
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    digest = base64.b64encode(h.digest()).decode("ascii")
    return f"{HASH_ALGO}-{digest}"

def find_hashed_files() -> dict:
    # Prefer dist/assets/ if present, else dist/
    assets_dir = DIST_DIR
    if (DIST_DIR / "assets").exists():
        assets_dir = DIST_DIR / "assets"

    if not assets_dir.exists():
        print("No dist/ or dist/assets/ found. Run your bundler first.", file=sys.stderr)
        sys.exit(1)

    manifest = {}
    for f in assets_dir.iterdir():
        if f.is_file() and f.suffix in INCLUDE_EXTS:
            # Logical name: main.js / main.css (strip content hash)
            name = f.name
            base = re.sub(r"\.[a-f0-9]{6,}\.", ".", name)
            logical = re.sub(r"^[^a-zA-Z]*", "", base)
            manifest[logical] = {
                "file": str(f.relative_to(DIST_DIR)).replace("\\", "/"),
                "integrity": hash_file(f),
                "size": f.stat().st_size,
            }
    return manifest

def main() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    manifest = find_hashed_files()
    if not manifest:
        print("No JS/CSS assets found in dist/.", file=sys.stderr)
        sys.exit(1)

    # Deterministic output
    with MANIFEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Manifest written ({len(manifest)} entries).")

if __name__ == "__main__":
    main()
```

```html
<!-- vanguard/frontend/templates/index.html -->
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <!-- Server-side templating should inject manifest values.
       Example (pseudo): if manifest.main.css -> inject -->
  {% if manifest and manifest['main.css'] %}
  <link rel="stylesheet" href="/{{ manifest['main.css'].file }}"
        integrity="{{ manifest['main.css'].integrity }}"
        crossorigin="anonymous">
  {% endif %}
</head>
<body>
  <!-- Strict mount point -->
  <div id="app" data-mount="vanguard"></div>

  {% if manifest and manifest['main.js'] %}
  <script src="/{{ manifest['main.js'].file }}"
          integrity="{{ manifest['main.js'].integrity }}"
          crossorigin="anonymous"
          defer></script>
  {% endif %}

  <!-- Fallback client-side loader (useful for static preview/CDN-first) -->
  <script>
    (function () {
      var mount = document.getElementById('app');
      if (!mount) {
        console.error('Vanguard mount point #app missing. Aborting.');
        return;
      }

      // Try to fetch manifest and ensure main.js is present (defensive)
      fetch('/manifest.json', { credentials: 'omit' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (m) {
          if (!m || !m['main.js']) return;
          if (document.querySelector('script[src*="main.js"]')) return;
          var s = document.createElement('script');
          s.src = '/' + m['main.js'].file;
          s.integrity = m['main.js'].integrity;
          s.crossOrigin = 'anonymous';
          s.defer = true;
          document.head.appendChild(s);
        })
        .catch(function () {});
    })();
  </script>
</body>
</html>
```

```javascript
// vanguard/frontend/entrypoint.js
// Enforce mount boundary and deterministic bootstrap.
(function () {
  'use strict';
  var APP_ID = 'app';
  var mount = document.getElementById(APP_ID);
  if (!mount) {
    throw new Error('Vanguard: mount point "#' + APP_ID + '" not found.');
  }

  // Safe runtime attachment
  window.__VANGUARD__ = window.__VANGUARD__ || {};
  window.__VANGUARD__.mount = mount;
  window.__VANGUARD__.ready = true;

  // Framework-agnostic bootstrap event
  try {
    document.dispatchEvent(new CustomEvent('vanguard:ready', {
      detail: { mount: mount }
    }));
  } catch (e) {
    // ignore in environments without CustomEvent
  }
})();
```

---

## Verification (actionable checklist)

1. Build and generate
   ```bash
   cd /opt/axentx/vanguard/frontend
   chmod +x build.sh
   ./build.sh
   ```
2. Confirm outputs
   - `dist/manifest.json` exists, is valid JSON, and every entry has `file`, `integrity`, and `size`.
   - `dist/` contains hashed JS/CSS assets referenced by manifest.
3. Serve and inspect
   - Serve `dist/` (e.g., `python3 -
