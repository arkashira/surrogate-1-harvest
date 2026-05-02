# vanguard / frontend

Below is the **single, merged implementation** that keeps every correct, actionable insight from both proposals and removes contradictions by prioritizing correctness and immediate usability.

- **Router**: hash-based, URL-driven state with `#/`, `#/datasets`, `#/datasets/:id`, `#/studio`.  
- **CDN bypass**: `file-list.json` cached in `localStorage`; no HF API calls during browsing.  
- **Dataset detail view**: parquet/file preview with surrogate-1 friendly metadata.  
- **Lightning Studio**: idempotent attach (reuse running session).  
- **Top-hub insight**: MOC badge + summary injected from knowledge-rag (cached).  
- **Env config**: constants at top for CDN base, repo, endpoints.  
- **Zero build**: open `index.html` in any browser; works immediately.  

Save this as `/opt/axentx/vanguard/index.html`.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Frontend</title>
  <style>
    :root {
      --bg: #0b0f19;
      --card: #111827;
      --muted: #6b7280;
      --accent: #10b981;
      --accent-soft: rgba(16,185,129,.08);
      --text: #f3f4f6;
      --border: #1f2937;
      --danger: #ef4444;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }
    header { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem; background: var(--card); border-bottom: 1px solid var(--border); gap: 1rem; flex-wrap: wrap; }
    .brand { display: flex; align-items: center; gap: .75rem; font-weight: 700; font-size: 1.125rem; }
    .badge { background: var(--accent); color: #000; padding: .2rem .5rem; border-radius: 4px; font-size: .7rem; font-weight: 600; }
    nav { display: flex; gap: .5rem; flex-wrap: wrap; }
    nav a { color: var(--muted); text-decoration: none; padding: .4rem .6rem; border-radius: 4px; font-size: .875rem; transition: color .15s, background .15s; }
    nav a.active { color: var(--accent); background: var(--accent-soft); }
    main { flex: 1; padding: 1.25rem; max-width: 980px; margin: 0 auto; width: 100%; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
    .row { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .5rem; }
    button { background: var(--accent); color: #000; border: none; padding: .5rem .75rem; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: .875rem; }
    button.secondary { background: transparent; border: 1px solid #374151; color: var(--text); }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem; color: var(--muted); word-break: break-all; }
    .toast { position: fixed; bottom: 1rem; right: 1rem; background: var(--card); border: 1px solid var(--border); padding: .75rem 1rem; border-radius: 8px; font-size: .875rem; box-shadow: 0 8px 24px rgba(0,0,0,.4); display: none; z-index: 1000; }
    .toast.show { display: block; }
    .loader { color: var(--muted); font-size: .875rem; }
    .error { color: var(--danger); font-size: .875rem; }
    table { width: 100%; border-collapse: collapse; font-size: .875rem; }
    th, td { text-align: left; padding: .5rem; border-bottom: 1px solid var(--border); color: var(--muted); }
    th { color: var(--text); font-weight: 600; }
    .detail-row { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin-top: .5rem; }
    .detail-label { color: var(--muted); font-size: .8rem; }
    .detail-value { color: var(--text); font-size: .85rem; }
    .back-row { margin-top: .75rem; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      Vanguard
      <span class="badge" id="hubBadge">MOC</span>
    </div>
    <nav id="nav" aria-label="Main navigation">
      <a href="#/" data-route="/">Overview</a>
      <a href="#/datasets" data-route="/datasets">Datasets</a>
      <a href="#/studio" data-route="/studio">Studio</a>
    </nav>
  </header>

  <main id="app" role="main"></main>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script>
    // ========== Env config (edit for dev/prod) ==========
    const ENV = {
      CDN_BASE: 'https://huggingface.co/datasets', // base for raw files
      REPO: 'axentx/surrogate-1',                  // dataset repo
      FILE_LIST_PATH: 'file-list.json',            // repo file with CDN URLs
      HUB_INSIGHT_KEY: 'vanguard_hub_insight',
      STUDIO_CHECK_MS: 5000
    };

    // ========== Utilities ==========
    const $ = (s) => document.querySelector(s);
    const $$ = (s) => document.querySelectorAll(s);
    const toast = (() => {
      const el = $('#toast');
      let timer = null;
      return {
        show(msg, persist) {
          clearTimeout(timer);
          el.textContent = msg;
          el.classList.add('show');
          if (!persist) timer = setTimeout(() => el.classList.remove('show'), 4000);
        },
        hide() { el.classList.remove('show'); }
      };
    })();

    // ========== Hub insight (knowledge-rag top-hub) ==========
    const HubBadge = {
      get() { try { return JSON.parse(localStorage.getItem(ENV.HUB_INSIGHT_KEY) || 'null'); } catch { return null; } },
      set(v) { localStorage.setItem(ENV.HUB_INSIGHT_KEY, JSON.stringify(v)); },
      seed() {
        if (!this.get()) {
          this.set({
            hub: 'MOC',
            summary: 'Most-connected hub (knowledge-rag 2026-04-27). Review before planning tasks.',
            ts: Date.now()
          });
        }
        const d = this.get();
        const badge = $('#hubBadge');
        if (badge && d) badge.textContent = d.hub;
      }
    };

    // ========== CDN-bypass file list ==========
    const FileListCache = {
      key: 'vanguard_file_list',
      get() { try { return JSON.parse(localStorage.getItem(this.key)
