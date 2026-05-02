# vanguard / frontend

## Final synthesized implementation

**Diagnosis (resolved)**  
- Missing canonical entrypoint and hub-first orientation → create `/opt/axentx/vanguard/index.html` as the single mount.  
- Missing top-hub (MOC) insight surface → add a persistent, refreshable MOC panel keyed to `#knowledge-rag #graph #hub`.  
- HF CDN-bypass file-list flow missing → implement a non-recursive date-folder lister that produces `file-list.json` using CDN URLs to avoid 429s.  
- No routing/navigation → add hash-based router with “Hub / Ingest / Train” and deep-linkable routes (`#/hub`, `#/ingest`, `#/train`).  
- No lightweight orchestration entrypoint → provide `orchestrate.sh` to list once, cache `file-list.json`, and invoke Lightning Studio reuse idempotently with L40S.

**Key choices for correctness + actionability**  
- Use CDN URLs (`https://cdn-lfs.huggingface.co/...`) for file listing to bypass HF API limits.  
- Keep frontend static; no server changes.  
- Make Lightning Studio reuse idempotent: prefer running studio, fallback to create, then attach/run training command with cached `file-list.json`.  
- Provide clear local run path: `./orchestrate.sh` handles listing, caching, and studio reuse in one step.

---

### Create files

```bash
cd /opt/axentx/vanguard
mkdir -p static
```

#### index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Knowledge Hub</title>
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body>
  <header class="topbar">
    <h1>Vanguard</h1>
    <nav>
      <a href="#/hub" data-page="hub" class="active">Hub</a>
      <a href="#/ingest" data-page="ingest">Ingest</a>
      <a href="#/train" data-page="train">Train</a>
    </nav>
  </header>

  <main id="app">
    <!-- Hub -->
    <section id="page-hub" class="page">
      <div class="card hub-card">
        <h2>Top Hub: MOC</h2>
        <p class="insight">Review the most-connected hub (MOC) before planning tasks. Use contextual insights from knowledge-rag to prioritize.</p>
        <div class="tags">#knowledge-rag #graph #hub</div>
        <div class="row">
          <button id="refresh-insight">Refresh Insight</button>
          <button id="list-hub-files" class="ghost">List Hub Files</button>
        </div>
        <pre id="insight-body" class="insight-body">Click "Refresh Insight" to load MOC summary.</pre>
      </div>
    </section>

    <!-- Ingest -->
    <section id="page-ingest" class="page hidden">
      <div class="card">
        <h2>HF CDN-bypass File List</h2>
        <p>List one date folder (non-recursive) and save file-list.json for training. Uses CDN URLs to avoid HF API rate limits.</p>
        <form id="ingest-form">
          <label>
            HF Repo (datasets/owner/repo)
            <input name="repo" type="text" placeholder="datasets/axentx/surrogate-1" required />
          </label>
          <label>
            Date folder (e.g. 2026-05-02)
            <input name="folder" type="text" placeholder="2026-05-02" required />
          </label>
          <div class="row">
            <button type="submit">Generate file-list.json</button>
            <button type="button" id="use-cached" class="ghost">Use Cached file-list.json</button>
          </div>
        </form>
        <div id="ingest-status" class="status"></div>
        <pre id="file-list-output" class="output"></pre>
      </div>
    </section>

    <!-- Train -->
    <section id="page-train" class="page hidden">
      <div class="card">
        <h2>Training Orchestration</h2>
        <p>Reuse or start a Lightning L40S studio idempotently and run training using CDN file-list.</p>
        <div class="row">
          <button id="list-studios">List Studios</button>
          <button id="reuse-studio">Reuse/Start L40S Studio</button>
        </div>
        <div id="studio-list" class="output"></div>
        <div id="train-status" class="status"></div>
        <pre id="train-output" class="output"></pre>
      </div>
    </section>
  </main>

  <footer class="footer">
    <small>Patterns: #knowledge-rag #graph #hub | HF CDN-bypass | Lightning reuse</small>
  </footer>

  <script src="/static/app.js"></script>
</body>
</html>
```

#### static/style.css
```css
:root{
  --bg:#0b1020;
  --card:#0f1724;
  --card2:#131b2e;
  --muted:#94a3b8;
  --accent:#60a5fa;
  --accent2:#22d3ee;
  --text:#e2e8f0;
  --danger:#f87171;
}
*{box-sizing:border-box}
body{
  margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial;
  background:var(--bg);color:var(--text);min-height:100vh;
}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 24px;background:var(--card);border-bottom:1px solid #1e293b;
}
.topbar h1{margin:0;font-size:18px}
.topbar nav a{
  margin-left:16px;color:var(--muted);text-decoration:none;font-size:14px;
}
.topbar nav a.active{color:var(--accent)}
#app{padding:24px;max-width:900px;margin:0 auto}
.page{display:block}
.page.hidden{display:none}
.card{
  background:var(--card2);padding:20px;border-radius:8px;
  border:1px solid #1e293b;
}
.hub-card .insight{color:var(--accent2);margin:8px 0}
.tags{font-size:12px;color:var(--muted);margin-bottom:8px}
.insight-body{background:#071028;padding:12px;border-radius:6px;overflow:auto;max-height:320px;color:#cbd5e1;white-space:pre-wrap;font-size:13px}
form{display:flex;flex-direction:column;gap:8px;margin-top:12px}
form input{padding:8px 10px;border-radius:6px;border:1px solid #334155;background:#0f1724;color:var(--text)}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}
button{
  padding:8px 14px;border-radius:6px;border:1px solid #3b82f6;
  background:#2563eb;color:#fff;cursor:pointer;font-size:14px;
}
button.ghost{
  background:transparent;border-color:#334155;color:var(--muted);
}
button:hover{opacity:0.9}
button:disabled{opacity:0.5;cursor:not-allowed}
.output{background:#071028;padding:12px;border-radius:6px;overflow:auto;max-height:400px;margin-top:12px;font-size:13
