# vanguard / quality

## 1. Diagnosis
- No deterministic asset manifest → frontend cannot reliably resolve dataset files; every dev cycle risks 404s or stale references.
- No build-time generation step → dataset file lists are fetched ad-hoc at runtime, exposing the app to HF API rate limits and inconsistent CDN availability.
- Missing frontend mount point and minimal Vite setup → no HMR, no type-safe imports, fragile path resolution, and slow iteration.
- No explicit CDN-first strategy in code → training/inference scripts may still attempt `load_dataset` or authenticated API calls instead of using `resolve/main/` URLs.
- No lightweight verification harness → cannot confirm manifest generation or CDN accessibility without running full training pipelines.

## 2. Proposed change
Add a build-time manifest generator and a minimal Vite frontend that consumes it, strictly using CDN URLs (`resolve/main/`) for all dataset file access. Scope:
- `/opt/axentx/vanguard/scripts/generate-manifest.py` (new)
- `/opt/axentx/vanguard/frontend/index.html` (new)
- `/opt/axentx/vanguard/frontend/src/main.js` (new)
- `/opt/axentx/vanguard/frontend/src/App.css` (new)
- `/opt/axentx/vanguard/frontend/package.json` (new)
- `/opt/axentx/vanguard/frontend/vite.config.js` (new)

## 3. Implementation

```bash
# Create project structure
mkdir -p /opt/axentx/vanguard/{scripts,frontend/src}
cd /opt/axentx/vanguard
```

### scripts/generate-manifest.py
```python
#!/usr/bin/env python3
"""
Generate a static manifest of dataset file paths for CDN-first access.
Run on Mac orchestration host (once per dataset update).
Outputs: frontend/public/manifest.json
"""
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_REPO = "datasets/your-org/your-dataset"  # <- update per project
OUTPUT_DIR = Path(__file__).parent.parent / "frontend" / "public"
OUTPUT_FILE = OUTPUT_DIR / "manifest.json"

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Single API call: list top-level folder (non-recursive) to avoid pagination/rate-limits
    # If deeper structure needed, list per subfolder and merge.
    items = list_repo_tree(repo_id=HF_REPO, path="", recursive=False)

    # Keep only files we want to expose (parquet/jsonl/csv). Adjust as needed.
    files = [
        {
            "path": item.path,
            "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{item.path}"
        }
        for item in items
        if item.type == "file" and item.path.lower().endswith((".parquet", ".jsonl", ".csv"))
    ]

    manifest = {
        "repo": HF_REPO,
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "files": files
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {OUTPUT_FILE} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

### frontend/package.json
```json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "devDependencies": {
    "vite": "^5.0.0"
  }
}
```

### frontend/vite.config.js
```js
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true
  },
  server: {
    port: 5173
  }
});
```

### frontend/index.html
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Vanguard — CDN-first</title>
    <link rel="stylesheet" href="/src/App.css" />
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
```

### frontend/src/App.css
```css
:root { font-family: system-ui, -apple-system, sans-serif; color-scheme: light dark; }
body { margin: 0; padding: 1.5rem; background: #f7f7fb; color: #111; }
#app { max-width: 900px; margin: 0 auto; }
.card { background: #fff; border: 1px solid #e6e6ef; border-radius: 8px; padding: 1rem; margin-bottom: 0.75rem; }
.meta { font-size: 0.85rem; color: #666; margin-bottom: 0.5rem; }
a.file-link { color: #2563eb; text-decoration: none; }
a.file-link:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; background: #eef2ff; color: #3730a3; font-size: 0.75rem; }
```

### frontend/src/main.js
```js
import './App.css';

const app = document.getElementById('app');

function createEl(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'className') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  });
  children.forEach(c => el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
  return el;
}

async function loadManifest() {
  try {
    const res = await fetch('/manifest.json', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error('Failed to load manifest:', err);
    return null;
  }
}

function render(manifest) {
  app.innerHTML = '';
  const header = createEl('div', { className: 'card' }, [
    createEl('div', { className: 'meta' }, [`Repo: ${manifest.repo}`]),
    createEl('div', { className: 'meta' }, [`Generated: ${manifest.generated_at}`]),
    createEl('h1', {}, ['Vanguard — Dataset Files (CDN-first)']),
    createEl('p', {}, [
      'All files are served via CDN (resolve/main/) — no authenticated API calls during frontend access.'
    ])
  ]);
  app.appendChild(header);

  const list = createEl('div', {}, []);
  manifest.files.forEach(f => {
    const ext = f.path.split('.').pop().toLowerCase();
    const card = createEl('div', { className: 'card' }, [
      createEl('div', { className: 'meta' }, [
        createEl('span', { className: 'badge' }, [ext]),
        ' — ',
        f.path
      ]),
      createEl('div', {}, [
        createEl('a', {
          href: f.cdn_url,
          target: '_blank',
          rel: 'noopener noreferrer',
          className: 'file-link'
        }, ['Open via CDN (resolve/main/)'])
      ])
    ]);
    list.appendChild(card);
  });

  if (manifest.files.length === 0) {
    list.appendChild(createEl('div', { className: 'card
