# vanguard / frontend

## Final synthesized implementation (correct + actionable)

**Core decision**: Use Vite + React + TypeScript. Embed a pre-computed file manifest at build time so the frontend never calls the Hugging Face Hub API from the browser (avoids 429s and quota burn). Provide a dev server with hot reload for fast iteration.

### 1. Create project skeleton and tooling

```bash
cd /opt/axentx/vanguard
```

```bash
# package.json
cat > package.json <<'JSON'
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0"
  },
  "devDependencies": {
    "vite": "^5.0.0",
    "@vitejs/plugin-react": "^4.0.0",
    "typescript": "^5.0.0",
    "@types/react": "^18.2.0",
    "@types/react-dom": "^18.2.0"
  }
}
JSON
```

```bash
# vite.config.ts
cat > vite.config.ts <<'TS'
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true
  },
  define: {
    __APP_ENV__: JSON.stringify(process.env.NODE_ENV || 'development')
  }
});
TS
```

```bash
# tsconfig.json
cat > tsconfig.json <<'JSON'
{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["DOM", "ES2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": false,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"]
}
JSON
```

```bash
# index.html
cat > index.html <<'HTML'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1.0" />
    <title>Vanguard</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
HTML
```

### 2. App source (React + TypeScript)

```bash
mkdir -p src
```

```bash
# src/main.tsx
cat > src/main.tsx <<'TSX'
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

const root = createRoot(document.getElementById('root')!);
root.render(<App />);
TSX
```

```bash
# src/types.ts
cat > src/types.ts <<'TS'
export interface DatasetFile {
  path: string;
  url: string;
  size?: number;
}
TS
```

```bash
# src/config.ts
cat > src/config.ts <<'TS'
// Embed pre-computed file manifest (CDN-only URLs).
// Generate once on the Mac (after HF API window clears) via:
//   huggingface_hub.list_repo_tree(repo_id, recursive=true)
// Save as JSON and paste below. This avoids runtime HF API calls from the browser.
//
// Example (replace with your actual repo/date/slugs):
// const DATASET_FILES: DatasetFile[] = [
//   { path: "datasets/myrepo/2026-04-29/sample-001.parquet", url: "https://huggingface.co/datasets/myrepo/resolve/main/2026-04-29/sample-001.parquet", size: 1234567 }
// ];

const DATASET_FILES: DatasetFile[] = [];

export function getDatasetFileUrls(): DatasetFile[] {
  return DATASET_FILES;
}

export function cdnUrl(repo: string, filePath: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
}
TS
```

```bash
# src/App.tsx
cat > src/App.tsx <<'TSX'
import React, { useEffect, useState } from 'react';
import { getDatasetFileUrls } from './config';
import './styles.css';

export default function App() {
  const [files, setFiles] = useState<Array<{ path: string; url: string; size?: number }>>([]);
  const [status, setStatus] = useState<'idle' | 'checking' | 'done'>('idle');
  const [checkResults, setCheckResults] = useState<Array<{ url: string; ok: boolean; size?: string }>>([]);

  useEffect(() => {
    setFiles(getDatasetFileUrls());
  }, []);

  const prefetchAndCheck = async () => {
    setStatus('checking');
    setCheckResults([]);
    const subset = files.slice(0, 3);
    const results = await Promise.allSettled(
      subset.map(async (f) => {
        const res = await fetch(f.url, { method: 'HEAD', cache: 'no-store' });
        return {
          url: f.url,
          ok: res.ok,
          size: res.headers.get('content-length') || undefined
        };
      })
    );
    setCheckResults(
      results.map((r) =>
        r.status === 'fulfilled'
          ? r.value
          : { url: 'error', ok: false, size: String(r.reason) }
      )
    );
    setStatus('done');
  };

  return (
    <div className="app">
      <header>
        <h1>Vanguard — Frontend</h1>
        <p>CDN-only dataset access (no runtime HF API calls from browser).</p>
      </header>

      <section>
        <button onClick={prefetchAndCheck} disabled={files.length === 0 || status === 'checking'}>
          {status === 'checking' ? 'Checking CDN...' : 'Prefetch sample files (HEAD)'}
        </button>
        <span className="status">{status === 'done' ? 'Done (see console & list below)' : ''}</span>
      </section>

      {checkResults.length > 0 && (
        <section className="check-results">
          <h3>CDN HEAD checks</h3>
          <ul>
            {checkResults.map((r, i) => (
              <li key={i} className={r.ok ? 'ok' : 'fail'}>
                <a href={r.url} target="_blank" rel="noopener noreferrer">{r.url}</a>
                <span>{r.ok ? 'OK' : 'FAIL'}</span>
                {r.size && <small> ~{r.size} bytes</small>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="file-list">
        <h2>Embedded file manifest ({files.length})</h2>
        {files.length === 0 ? (
          <p>No files in manifest — update src/config.ts with your repo/date/slugs.</p>
        ) : (
          <ul>
            {files.map((f, i) => (
              <li key={i}>
                <a href={f.url} target="_blank" rel="noopener noreferrer">{f.path}</a>
                {f.size != null && <small> (~{f.size}
