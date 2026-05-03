# vanguard / frontend

## 1. Diagnosis

- No frontend build/dev tooling or entrypoint exists in `/opt/axentx/vanguard` — cannot ship or iterate on UI.
- Missing `index.html` and client mount point means no fast feedback loop for frontend changes.
- No asset pipeline or bundler configured; any JS/TS/CSS would require manual reloads and offer no module resolution.
- No dev server or hot-reload — frontend iteration would rely on file refresh only, slowing UI work.
- Absence of a minimal app shell prevents incremental UI feature development (components, routing, state).

## 2. Proposed change

Add a minimal, production-ready frontend scaffold:
- `/opt/axentx/vanguard/index.html` — app shell with mount point and bundled script tag.
- `/opt/axentx/vanguard/src/main.js` — lightweight app bootstrap (DOM mount + sample UI).
- `/opt/axentx/vanguard/src/styles.css` — baseline styles and responsive layout.
- `/opt/axentx/vanguard/package.json` + `vite.config.js` — dev server + build tooling (fast HMR).
- `/opt/axentx/vandaland/.gitignore` — exclude node_modules and build artifacts.

Scope: add files only; no changes to existing backend code.

## 3. Implementation

```bash
cd /opt/axentx/vanguard

# package.json
cat > package.json <<'EOF'
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
EOF

# vite.config.js
cat > vite.config.js <<'EOF'
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  server: {
    port: 5173,
    open: true
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
});
EOF

# index.html
cat > index.html <<'EOF'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Vanguard</title>
    <link rel="stylesheet" href="/src/styles.css" />
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
EOF

# src/styles.css
mkdir -p src
cat > src/styles.css <<'EOF'
:root {
  --bg: #0f172a;
  --card: #1e293b;
  --accent: #38bdf8;
  --text: #f1f5f9;
  --muted: #94a3b8;
  --radius: 10px;
  --max-width: 900px;
}

* { box-sizing: border-box; }
html,body,#app { height: 100%; margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; background: var(--bg); color: var(--text); }

.app-shell {
  max-width: var(--max-width);
  margin: 48px auto;
  padding: 24px;
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: 0 6px 24px rgba(2,6,23,0.6);
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.brand { display: flex; align-items: center; gap: 12px; }
.logo { width: 40px; height: 40px; background: var(--accent); border-radius: 8px; }
.title { font-size: 1.125rem; font-weight: 700; color: var(--text); }

.status-badge {
  font-size: 0.75rem;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(56,189,248,0.12);
  color: var(--accent);
  border: 1px solid rgba(56,189,248,0.18);
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.card {
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.04);
  padding: 16px;
  border-radius: 10px;
  min-height: 80px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.card .label { font-size: 0.75rem; color: var(--muted); margin-bottom: 6px; }
.card .value { font-size: 1.125rem; font-weight: 600; color: var(--text); }

.btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: 8px;
  border: none;
  background: var(--accent);
  color: #0f172a;
  font-weight: 600;
  cursor: pointer;
  transition: transform .12s ease, box-shadow .12s ease;
}
.btn:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(56,189,248,0.25); }

@media (max-width: 640px) {
  .app-shell { margin: 16px; padding: 16px; }
}
EOF

# src/main.js
cat > src/main.js <<'EOF'
import './styles.css';

const App = () => {
  const shell = document.createElement('div');
  shell.className = 'app-shell';

  shell.innerHTML = `
    <header class="header">
      <div class="brand">
        <div class="logo"></div>
        <div class="title">Vanguard</div>
      </div>
      <div class="status-badge" id="statusBadge">Idle</div>
    </header>

    <p style="color:var(--muted);font-size:0.9rem;margin:0 0 12px 0;">
      Minimal frontend scaffold — ready for components and API integrations.
    </p>

    <div class="card-grid" id="cardGrid">
      <div class="card"><div class="label">Status</div><div class="value" id="statusVal">—</div></div>
      <div class="card"><div class="label">Last run</div><div class="value" id="lastRunVal">—</div></div>
      <div class="card"><div class="label">Queue</div><div class="value" id="queueVal">0</div></div>
      <div class="card"><div class="label">Version</div><div class="value" id="versionVal">0.1.0</div></div>
    </div>

    <div style="margin-top:18px;">
      <button class="btn" id="refreshBtn" type="button">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 4v6h-6M1 20v-
