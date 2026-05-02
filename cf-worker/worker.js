// surrogate-1-cursor — multi-purpose CF Worker
//
// Handlers:
//   fetch()      — HTTP API (cursor service, datasets, audit, metrics, dashboard, status, admin)
//   scheduled()  — runs every 5 min: pings 6 HF Spaces /health → space_health
//                  runs every 15 min: end-to-end canary (cursor read/advance/audit) → canary_runs
//   queue()      — consumes surrogate-1-tasks (3rd queue backend, round-robin)
//
// Bindings: env.DB (D1), env.CACHE (KV), env.AUTH_TOKEN, env.HF_TOKEN,
//           env.TASKS_QUEUE (producer), env.AI (Workers AI)
//
// Roadmap features in this version:
//   #1   cursor exhaustion + total tracking
//   #2   Worker auth (shared secret on writes/audit)
//   #9   audit log
//   #15  external health pinger (cron + HF Space pings)
//   #21  /metrics (Prom format)
//   #22  per-dataset dashboard (basic HTML at /dash)
//   #36  synthetic canary every 15 min  (NEW)
//   #39  D1 + Supabase backup to HF Hub at /admin/backup  (NEW)
//   #42  distributed tracing UI on /dash/trace/<trace_id>  (NEW)
//   #44  public /status page  (NEW)
//   #67  KV cache hit-rate tracking (kv_hit / kv_miss metrics)  (NEW)
//   #70  D1 → KV mirror for hot reads (datasets + space_health, 30s TTL)  (NEW)
//   #76  per-IP rate limit (100/min via KV sliding window)  (NEW)
//   #77  audit_log immutability (D1 triggers preventing UPDATE/DELETE)  (NEW)
//   #92  landing page assets served by Pages, /metrics consumed live      (related)
//   #99  pricing A/B harness — KV experiment config + click tracking      (NEW)
//   CF-#A  Workers AI (callable from /ai/<model> for testing)
//   CF-#C  Queue consumer (push from anywhere via env.TASKS_QUEUE.send())

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Auth-Token, X-Trace-Id",
};

const SPACES = [
  "axentx/surrogate-1",
  "surrogate1/surrogate-1-shard2",
  "surrogate1/surrogate-1-zero-gpu",
  "ashirafuse1/surrogate-1-shard3",
  "ashirato/surrogate-1-zero-gpu",
  "ashirato/surrogate-1-shard1",
];

const RATE_LIMIT_PER_MIN = 100;
const RATE_LIMIT_EXEMPT_PATHS = new Set(["/health", "/", "/status", "/metrics"]);

const json = (obj, status = 200, extraHeaders = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extraHeaders },
  });

function authed(request, env) {
  const want = (env.AUTH_TOKEN || "").trim();
  if (!want) return true;
  const got = (
    request.headers.get("X-Auth-Token") ||
    (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "")
  ).trim();
  return got && got === want;
}

function newTraceId() {
  // 16 hex chars; sufficient for cross-service correlation
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

async function audit(env, ctx, action, slug, meta, traceId) {
  ctx.waitUntil(
    env.DB.prepare(
      "INSERT INTO audit_log (action, dataset_id, meta, trace_id, ts) VALUES (?1, ?2, ?3, ?4, unixepoch())"
    )
      .bind(action, slug || null, JSON.stringify(meta || {}).slice(0, 2000), traceId || null)
      .run()
      .catch(() => {})
  );
}

async function bumpMetric(env, ctx, key, by = 1) {
  ctx.waitUntil(
    env.DB.prepare(
      "INSERT INTO metrics (key, n) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET n = n + ?2"
    ).bind(key, by).run().catch(() => {})
  );
}

// #67 — wrap KV reads, count hit/miss
async function kvGetTracked(env, ctx, key, route) {
  const v = await env.CACHE.get(key, { type: "json" });
  const status = v == null ? "miss" : "hit";
  ctx.waitUntil(bumpMetric(env, ctx, `kv_${status}:${route}`));
  return v;
}

// #76 — per-IP rate limit using KV sliding window (60s)
async function rateLimit(request, env, ctx, path) {
  if (RATE_LIMIT_EXEMPT_PATHS.has(path)) return null;
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const bucket = Math.floor(Date.now() / 60000);
  const key = `rl:${ip}:${bucket}`;
  const cur = parseInt((await env.CACHE.get(key)) || "0");
  if (cur >= RATE_LIMIT_PER_MIN) {
    ctx.waitUntil(bumpMetric(env, ctx, "ratelimit:blocked"));
    return json(
      { error: "rate_limited", limit: RATE_LIMIT_PER_MIN, window_seconds: 60 },
      429,
      { "Retry-After": "60" }
    );
  }
  ctx.waitUntil(env.CACHE.put(key, String(cur + 1), { expirationTtl: 90 }));
  return null;
}

// ── Dashboard HTML (server-rendered, no JS framework) ──────────────────────
function escape(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderDashboard(stats, datasets, spaces, audit, recentTraces) {
  return `<!doctype html><html><head>
<meta charset="utf-8"><title>surrogate-1 dashboard</title>
<style>
  body{font:14px/1.5 system-ui,Arial;margin:24px;color:#222;max-width:1200px}
  h1{margin:0 0 8px}h2{margin:24px 0 6px;font-size:15px;color:#666}
  table{border-collapse:collapse;width:100%;margin:6px 0 18px}
  td,th{border-bottom:1px solid #eee;padding:5px 8px;text-align:left;font-size:13px}
  th{background:#fafafa;font-weight:600}
  .ok{color:#080}.bad{color:#c00}.muted{color:#888}
  .b{font-weight:600}.r{text-align:right;font-variant-numeric:tabular-nums}
  a{color:#06c;text-decoration:none}a:hover{text-decoration:underline}
  code{font:12px ui-monospace,monospace;background:#f6f6f6;padding:1px 4px;border-radius:3px}
</style></head><body>
<h1>surrogate-1 dashboard</h1>
<div class="muted">live state · ${escape(new Date().toISOString())}</div>
<div class="muted">links: <a href="/status">public status</a> · <a href="/metrics">prom metrics</a> · <a href="/audit">audit (auth)</a></div>

<h2>HF Spaces (last cron probe)</h2>
<table><tr><th>Space</th><th>Status</th><th class="r">Latency</th><th class="r">Last seen</th></tr>
${spaces.map(s => `<tr>
  <td>${escape(s.space_id)}</td>
  <td class="${s.http_code >= 200 && s.http_code < 400 ? 'ok' : 'bad'}">${escape(s.http_code || '?')}</td>
  <td class="r">${escape(s.latency_ms || '?')}ms</td>
  <td class="r">${s.ts ? escape(new Date(s.ts*1000).toISOString().slice(11,19)) : '?'}</td>
</tr>`).join('')}
</table>

<h2>Datasets registry (${datasets.length})</h2>
<table><tr><th>Score</th><th>Slug</th><th>HF ID</th><th class="r">Cap</th></tr>
${datasets.slice(0, 25).map(d => `<tr>
  <td>${escape(d.score?.toFixed(2))}</td>
  <td class="b">${escape(d.slug)}</td>
  <td>${escape(d.id)}</td>
  <td class="r">${escape(d.cap?.toLocaleString())}</td>
</tr>`).join('')}
</table>

<h2>Counters</h2>
<table>${Object.entries(stats).map(([k,v]) => `<tr><td><code>${escape(k)}</code></td><td class="r b">${escape(v.toLocaleString())}</td></tr>`).join('')}</table>

<h2>Recent traces (${recentTraces.length}) — click for span timeline</h2>
<table><tr><th>Trace</th><th>Spans</th><th>First action</th><th class="r">Started</th></tr>
${recentTraces.map(t => `<tr>
  <td><a href="/dash/trace/${escape(t.trace_id)}"><code>${escape(t.trace_id)}</code></a></td>
  <td class="r">${escape(t.span_count)}</td>
  <td>${escape(t.first_action)}</td>
  <td class="r muted">${escape(new Date(t.first_ts*1000).toISOString().slice(11,19))}</td>
</tr>`).join('')}
</table>

<h2>Recent audit (last 20)</h2>
<table><tr><th>When</th><th>Action</th><th>Subject</th><th>Trace</th><th>Meta</th></tr>
${audit.slice(0, 20).map(a => `<tr>
  <td class="muted">${escape(new Date(a.ts*1000).toISOString().slice(11,19))}</td>
  <td>${escape(a.action)}</td>
  <td>${escape(a.dataset_id || '')}</td>
  <td>${a.trace_id ? `<a href="/dash/trace/${escape(a.trace_id)}"><code>${escape(a.trace_id.slice(0,8))}</code></a>` : ''}</td>
  <td class="muted">${escape((a.meta || '').slice(0, 80))}</td>
</tr>`).join('')}
</table>
</body></html>`;
}

// #42 — span Gantt for one trace_id
function renderTraceGantt(traceId, rows) {
  if (!rows.length) {
    return `<!doctype html><body style="font:14px system-ui;margin:24px"><h1>trace ${escape(traceId)}</h1><p>no spans found</p><p><a href="/dash">← back</a></p></body>`;
  }
  const t0 = rows[0].ts;
  const tEnd = rows[rows.length - 1].ts;
  const span = Math.max(1, tEnd - t0);
  return `<!doctype html><html><head>
<meta charset="utf-8"><title>trace ${escape(traceId)}</title>
<style>
  body{font:13px/1.4 system-ui,Arial;margin:24px;max-width:1200px;color:#222}
  h1{margin:0 0 8px;font-size:18px}
  .gantt{margin:16px 0}
  .row{display:grid;grid-template-columns:160px 1fr 80px;gap:8px;align-items:center;padding:3px 0;border-bottom:1px solid #f0f0f0}
  .bar-track{height:18px;background:#fafafa;border-radius:3px;position:relative}
  .bar{position:absolute;top:2px;height:14px;background:#06c;border-radius:2px;min-width:2px}
  .meta{font:11px ui-monospace,monospace;color:#888;background:#f6f6f6;padding:8px;border-radius:4px;white-space:pre-wrap;max-height:100px;overflow:auto}
  details{margin:2px 0}summary{cursor:pointer}
  .r{text-align:right;font-variant-numeric:tabular-nums}
  a{color:#06c}
</style></head><body>
<h1>trace <code>${escape(traceId)}</code></h1>
<p class="muted">${rows.length} spans · ${span}s window · <a href="/dash">← dash</a></p>
<div class="gantt">
${rows.map((r, i) => {
  const offset = ((r.ts - t0) / span) * 100;
  const next = rows[i + 1]?.ts || r.ts + 1;
  const widthPct = Math.max(0.5, ((next - r.ts) / span) * 100);
  return `<div class="row">
    <div><b>${escape(r.action)}</b><br><span class="muted">${escape(r.dataset_id || '')}</span></div>
    <div class="bar-track"><div class="bar" style="left:${offset}%;width:${widthPct}%"></div></div>
    <div class="r muted">+${(r.ts - t0)}s</div>
  </div>
  <details><summary class="muted">meta</summary><div class="meta">${escape(r.meta || '')}</div></details>`;
}).join('')}
</div>
</body></html>`;
}

// #44 — public status page (no auth)
function renderStatus(spaces, canaryStats, slaStats) {
  return `<!doctype html><html><head>
<meta charset="utf-8"><title>surrogate-1 — system status</title>
<style>
  body{font:14px/1.6 system-ui,Arial;margin:0;color:#222;background:#fafafa}
  .wrap{max-width:900px;margin:0 auto;padding:32px 24px}
  h1{margin:0 0 4px;font-size:22px}
  .lede{color:#666;margin:0 0 24px}
  .card{background:#fff;border:1px solid #eee;border-radius:8px;padding:16px;margin:12px 0}
  table{border-collapse:collapse;width:100%;margin-top:8px}
  td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;font-size:13px}
  th{background:#fafafa;font-weight:600;color:#666}
  .ok{color:#0a0;font-weight:600}.bad{color:#c00;font-weight:600}.warn{color:#c80;font-weight:600}
  .pill{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600}
  .pill-ok{background:#e7f5e7;color:#080}
  .pill-bad{background:#fbe7e7;color:#c00}
  .pill-warn{background:#fbf3e0;color:#c80}
  .r{text-align:right;font-variant-numeric:tabular-nums}
  .muted{color:#888;font-size:12px}
</style></head><body>
<div class="wrap">
<h1>surrogate-1 — system status</h1>
<p class="lede">Live status of the autonomous AI dev fleet · <span class="muted">${escape(new Date().toISOString())}</span></p>

<div class="card">
  <h2>Overall</h2>
  <p>${slaStats.overallOk
    ? '<span class="pill pill-ok">All systems operational</span>'
    : '<span class="pill pill-bad">Degraded</span>'}</p>
</div>

<div class="card">
<h2>HF Spaces (last 24h)</h2>
<table><tr><th>Space</th><th>Status</th><th class="r">Last latency</th><th class="r">Last probe</th></tr>
${spaces.map(s => `<tr>
  <td>${escape(s.space_id)}</td>
  <td>${s.http_code >= 200 && s.http_code < 400
    ? '<span class="pill pill-ok">up</span>'
    : '<span class="pill pill-bad">down</span>'}</td>
  <td class="r">${escape(s.latency_ms || '?')}ms</td>
  <td class="r muted">${s.ts ? escape(new Date(s.ts*1000).toISOString().slice(0,19).replace('T',' ')) : '?'}</td>
</tr>`).join('')}
</table>
</div>

<div class="card">
<h2>End-to-end canary (last 24h)</h2>
<p>
  Success rate: <b>${escape(canaryStats.successRate.toFixed(1))}%</b> over ${escape(canaryStats.total)} runs<br>
  ${canaryStats.successRate >= 95
    ? '<span class="pill pill-ok">healthy</span>'
    : canaryStats.successRate >= 80
      ? '<span class="pill pill-warn">degraded</span>'
      : '<span class="pill pill-bad">failing</span>'}
  <span class="muted">· p95 latency: ${escape(canaryStats.p95Ms)}ms</span>
</p>
</div>

<p class="muted">Powered by Cloudflare Workers · D1 · KV. Source: <a href="https://github.com/axentx/surrogate-1">github</a></p>
</div>
</body></html>`;
}

// ── Backup helpers (#39) ────────────────────────────────────────────────────
async function dumpTable(env, table, limit = 50000) {
  const r = await env.DB.prepare(`SELECT * FROM ${table} ORDER BY rowid DESC LIMIT ?`).bind(limit).all();
  return r.results || [];
}

async function uploadToHF(env, ctx, path, content) {
  const repo = "axentx/surrogate-1-backups";
  const url = `https://huggingface.co/api/datasets/${repo}/upload/main/${path}`;
  // HF upload via simple file PUT API (LFS-aware endpoint).
  // Use the multi-file commit API to avoid LFS pointer overhead for small JSON.
  const commitUrl = `https://huggingface.co/api/datasets/${repo}/commit/main`;
  const fileB64 = btoa(unescape(encodeURIComponent(content)));
  const ndjson =
    JSON.stringify({ key: "header", value: { summary: `auto backup ${path}` } }) + "\n" +
    JSON.stringify({
      key: "file",
      value: { path, encoding: "base64", content: fileB64 },
    }) + "\n";
  const resp = await fetch(commitUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.HF_TOKEN}`,
      "Content-Type": "application/x-ndjson",
    },
    body: ndjson,
  });
  return { status: resp.status, ok: resp.ok, body: (await resp.text()).slice(0, 500) };
}

export default {
  // ── HTTP handler ─────────────────────────────────────────────────────────
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    const path = url.pathname;
    const t0 = Date.now();
    const traceId = request.headers.get("X-Trace-Id") || newTraceId();

    // #76 — rate limit (skip exempt paths)
    const limited = await rateLimit(request, env, ctx, path);
    if (limited) return limited;

    try {
      if (path === "/health" || path === "/") {
        await bumpMetric(env, ctx, "req:health");
        return json({ status: "ok", service: "surrogate-1-cursor", ts: Date.now() }, 200, {
          "X-Trace-Id": traceId,
        });
      }

      if (path === "/dynamic-datasets" && request.method === "GET") {
        await bumpMetric(env, ctx, "req:datasets");
        const cached = await kvGetTracked(env, ctx, "datasets:all", "datasets");
        if (cached) return json(cached, 200, { "X-Trace-Id": traceId });
        const r = await env.DB.prepare(
          "SELECT slug, hf_id AS id, schema, license, cap, score, downloads, discovered_ts FROM datasets ORDER BY score DESC LIMIT 5000"
        ).all();
        const list = r.results || [];
        ctx.waitUntil(env.CACHE.put("datasets:all", JSON.stringify(list), { expirationTtl: 60 }));
        return json(list, 200, { "X-Trace-Id": traceId });
      }

      if (path === "/metrics" && request.method === "GET") {
        const r = await env.DB.prepare("SELECT key, n FROM metrics ORDER BY key").all();
        const lines = [
          "# HELP surrogate_cursor_requests Total requests by endpoint",
          "# TYPE surrogate_cursor_requests counter",
        ];
        for (const m of (r.results || [])) {
          // Sanitize key for Prometheus label
          const safe = String(m.key).replace(/[^a-zA-Z0-9_:]/g, "_");
          lines.push(`surrogate_cursor_requests{key="${safe}"} ${m.n}`);
        }
        return new Response(lines.join("\n") + "\n", {
          headers: { "Content-Type": "text/plain; version=0.0.4", ...CORS },
        });
      }

      if (path === "/dash" && request.method === "GET") {
        // #70 — KV mirror for hot reads (datasets + space_health, 30s TTL)
        let datasets = await kvGetTracked(env, ctx, "dash:datasets", "dash_datasets");
        if (!datasets) {
          const dr = await env.DB.prepare(
            "SELECT slug, hf_id AS id, score, cap FROM datasets ORDER BY score DESC LIMIT 50"
          ).all();
          datasets = dr.results || [];
          ctx.waitUntil(env.CACHE.put("dash:datasets", JSON.stringify(datasets), { expirationTtl: 30 }));
        }
        let spaces = await kvGetTracked(env, ctx, "dash:spaces", "dash_spaces");
        if (!spaces) {
          const sr = await env.DB.prepare(
            "SELECT space_id, http_code, latency_ms, MAX(ts) as ts FROM space_health GROUP BY space_id ORDER BY space_id"
          ).all().catch(() => ({ results: [] }));
          spaces = sr.results || [];
          ctx.waitUntil(env.CACHE.put("dash:spaces", JSON.stringify(spaces), { expirationTtl: 30 }));
        }
        const [m, a, traces] = await Promise.all([
          env.DB.prepare("SELECT key, n FROM metrics").all(),
          env.DB.prepare("SELECT action, dataset_id, meta, trace_id, ts FROM audit_log ORDER BY id DESC LIMIT 20").all(),
          env.DB.prepare(
            "SELECT trace_id, COUNT(*) AS span_count, MIN(action) AS first_action, MIN(ts) AS first_ts " +
            "FROM audit_log WHERE trace_id IS NOT NULL " +
            "GROUP BY trace_id ORDER BY first_ts DESC LIMIT 10"
          ).all().catch(() => ({ results: [] })),
        ]);
        const stats = Object.fromEntries((m.results || []).map(x => [x.key, x.n]));
        return new Response(
          renderDashboard(stats, datasets, spaces, a.results || [], traces.results || []),
          { headers: { "Content-Type": "text/html; charset=utf-8", ...CORS, "X-Trace-Id": traceId } }
        );
      }

      // #42 — /dash/trace/<trace_id> Gantt view
      const traceMatch = path.match(/^\/dash\/trace\/([a-zA-Z0-9-]+)$/);
      if (traceMatch && request.method === "GET") {
        const tid = traceMatch[1];
        const r = await env.DB.prepare(
          "SELECT action, dataset_id, meta, ts FROM audit_log WHERE trace_id = ? ORDER BY id ASC LIMIT 200"
        ).bind(tid).all();
        return new Response(renderTraceGantt(tid, r.results || []), {
          headers: { "Content-Type": "text/html; charset=utf-8", ...CORS },
        });
      }

      // #44 — public /status (no auth)
      if (path === "/status" && request.method === "GET") {
        const since = Math.floor(Date.now() / 1000) - 86400;
        const [sr, cr] = await Promise.all([
          env.DB.prepare(
            "SELECT space_id, http_code, latency_ms, MAX(ts) AS ts FROM space_health WHERE ts >= ? GROUP BY space_id ORDER BY space_id"
          ).bind(since).all().catch(() => ({ results: [] })),
          env.DB.prepare(
            "SELECT success, latency_ms FROM canary_runs WHERE ts >= ?"
          ).bind(since).all().catch(() => ({ results: [] })),
        ]);
        const spaces = sr.results || [];
        const canary = cr.results || [];
        const total = canary.length;
        const okCount = canary.filter(c => c.success).length;
        const successRate = total ? (okCount / total) * 100 : 100;
        const lats = canary.filter(c => c.success).map(c => c.latency_ms).sort((a, b) => a - b);
        const p95Ms = lats.length ? lats[Math.floor(lats.length * 0.95)] || lats[lats.length - 1] : 0;
        const allSpacesUp = spaces.every(s => s.http_code >= 200 && s.http_code < 400);
        const overallOk = allSpacesUp && successRate >= 95;
        return new Response(
          renderStatus(spaces, { successRate, total, p95Ms }, { overallOk }),
          { headers: { "Content-Type": "text/html; charset=utf-8", ...CORS } }
        );
      }

      // /ai/<model> — proxy to Workers AI
      const aiMatch = path.match(/^\/ai\/(.+)$/);
      if (aiMatch && request.method === "POST") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        const model = "@cf/" + decodeURIComponent(aiMatch[1]);
        const body = await request.json().catch(() => ({}));
        const result = await env.AI.run(model, {
          messages: body.messages || [{ role: "user", content: body.prompt || "" }],
          max_tokens: body.max_tokens || 512,
        });
        await bumpMetric(env, ctx, `ai:${model}`);
        return json(result, 200, { "X-Trace-Id": traceId });
      }

      // /tasks/push — enqueue into CF Queue
      if (path === "/tasks/push" && request.method === "POST") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        const body = await request.json().catch(() => ({}));
        await env.TASKS_QUEUE.send(body);
        await bumpMetric(env, ctx, "queue:push");
        await audit(env, ctx, "queue_push", body?.dataset_id, body, traceId);
        return json({ ok: true }, 200, { "X-Trace-Id": traceId });
      }

      // #39 — /admin/backup — dump tables → JSON → HF Hub
      if (path === "/admin/backup" && request.method === "POST") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        if (!env.HF_TOKEN) return json({ error: "HF_TOKEN not set" }, 500);
        const tables = ["cursors", "datasets", "audit_log", "space_health", "canary_runs", "metrics"];
        const dumps = {};
        for (const t of tables) {
          dumps[t] = await dumpTable(env, t).catch(e => ({ error: e.message }));
        }
        const ts = new Date().toISOString().replace(/[:.]/g, "-");
        const filePath = `backups/${ts.slice(0, 10)}/dump-${ts}.json`;
        const content = JSON.stringify(
          { generated_at: ts, source: "surrogate-1-cursor", tables: dumps },
          null,
          2
        );
        const result = await uploadToHF(env, ctx, filePath, content);
        await audit(env, ctx, "backup", null, {
          path: filePath, size: content.length, hf_status: result.status,
        }, traceId);
        await bumpMetric(env, ctx, "admin:backup");
        return json(
          { ok: result.ok, path: filePath, size_bytes: content.length, hf: result },
          result.ok ? 200 : 502,
          { "X-Trace-Id": traceId }
        );
      }

      // #99 — /experiments/<key>/active — return active variant config
      const expMatch = path.match(/^\/experiments\/([a-zA-Z0-9_-]+)\/active$/);
      if (expMatch && request.method === "GET") {
        const key = expMatch[1];
        const cfg = await kvGetTracked(env, ctx, `exp:${key}`, "experiment");
        if (!cfg) return json({ error: "no active experiment", key }, 404);
        return json(cfg, 200, { "X-Trace-Id": traceId });
      }

      // #99 — /experiments/<key>/click — track CTR (no auth needed; public landing)
      const clickMatch = path.match(/^\/experiments\/([a-zA-Z0-9_-]+)\/click$/);
      if (clickMatch && request.method === "POST") {
        const key = clickMatch[1];
        const b = await request.json().catch(() => ({}));
        const variant = (b.variant || "unknown").slice(0, 32);
        const target = (b.target || "").slice(0, 200);
        const ip = request.headers.get("CF-Connecting-IP") || "unknown";
        await env.DB.prepare(
          "INSERT INTO experiment_clicks (experiment_key, variant, target, ip_hash, ts) VALUES (?1, ?2, ?3, ?4, unixepoch())"
        ).bind(key, variant, target, ip.slice(0, 64)).run().catch(() => {});
        await bumpMetric(env, ctx, `exp:${key}:${variant}`);
        return json({ ok: true });
      }

      // #99 — admin: set active variant for experiment (auth)
      if (path === "/admin/experiments" && request.method === "POST") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        const b = await request.json().catch(() => ({}));
        if (!b.key || !Array.isArray(b.variants)) {
          return json({ error: "key and variants[] required" }, 400);
        }
        await env.CACHE.put(`exp:${b.key}`, JSON.stringify({
          key: b.key, variants: b.variants, set_at: Math.floor(Date.now() / 1000),
        }));
        await audit(env, ctx, "experiment_set", b.key, { variants: b.variants }, traceId);
        return json({ ok: true, key: b.key });
      }

      // Cursor routes
      const m = path.match(/^\/cursor\/([^\/]+)(\/advance)?\/?$/);
      if (m) {
        const slug = decodeURIComponent(m[1]);
        const isAdvance = !!m[2] && request.method === "POST";

        if (isAdvance) {
          if (!authed(request, env)) return json({ error: "auth required" }, 401);
          const b = await request.json().catch(() => ({}));
          const size = Math.max(1, Math.min(100000, parseInt(b.size || 1000)));
          const last = (b.last_batch || "").slice(0, 200);
          const total = b.total != null ? parseInt(b.total) : null;
          const exhausted = b.exhausted ? 1 : 0;
          const cur = await env.DB.prepare(
            "INSERT INTO cursors (dataset_id, offset, total, last_batch, exhausted) " +
            "VALUES (?1, ?2, ?3, ?4, ?5) " +
            "ON CONFLICT(dataset_id) DO UPDATE SET " +
            "  offset = offset + ?2, total = COALESCE(?3, total), last_batch = ?4, " +
            "  exhausted = MAX(exhausted, ?5), updated_at = unixepoch() " +
            "RETURNING dataset_id, offset, total, last_batch, exhausted, updated_at"
          ).bind(slug, size, total, last, exhausted).first();
          if (cur && cur.total != null && cur.offset >= cur.total && !cur.exhausted) {
            await env.DB.prepare("UPDATE cursors SET exhausted=1 WHERE dataset_id=?")
              .bind(slug).run();
            cur.exhausted = 1;
          }
          await bumpMetric(env, ctx, "req:advance");
          await audit(env, ctx, "advance", slug, { size, total, exhausted: cur?.exhausted }, traceId);
          return json(cur, 200, { "X-Trace-Id": traceId });
        }

        await bumpMetric(env, ctx, "req:cursor_read");
        let cur = await env.DB.prepare(
          "SELECT dataset_id, offset, total, last_batch, exhausted, updated_at FROM cursors WHERE dataset_id = ?"
        ).bind(slug).first();
        if (!cur) cur = { dataset_id: slug, offset: 0, total: null, last_batch: null, exhausted: 0, updated_at: null };
        await audit(env, ctx, "read", slug, { offset: cur.offset }, traceId);
        return json(cur, 200, { "X-Trace-Id": traceId });
      }

      if (path === "/datasets" && request.method === "POST") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        const b = await request.json().catch(() => ({}));
        if (!b.slug || !b.hf_id) return json({ error: "slug and hf_id required" }, 400);
        await env.DB.prepare(
          "INSERT INTO datasets (slug, hf_id, schema, license, cap, score) VALUES (?1,?2,?3,?4,?5,?6) " +
          "ON CONFLICT(slug) DO UPDATE SET hf_id=excluded.hf_id, schema=excluded.schema, license=excluded.license, cap=excluded.cap, score=excluded.score"
        ).bind(b.slug, b.hf_id, b.schema || "messages", b.license || null, b.cap || 50000, b.score || 0.5).run();
        ctx.waitUntil(env.CACHE.delete("datasets:all"));
        ctx.waitUntil(env.CACHE.delete("dash:datasets"));
        await audit(env, ctx, "register", b.slug, { hf_id: b.hf_id }, traceId);
        await bumpMetric(env, ctx, "req:datasets_upsert");
        return json({ ok: true, slug: b.slug }, 200, { "X-Trace-Id": traceId });
      }

      if (path === "/audit" && request.method === "GET") {
        if (!authed(request, env)) return json({ error: "auth required" }, 401);
        const limit = Math.min(500, parseInt(url.searchParams.get("limit") || "100"));
        const since = parseInt(url.searchParams.get("since") || "0");
        const tid = url.searchParams.get("trace_id");
        let sql, args;
        if (tid) {
          sql = "SELECT id, action, dataset_id, meta, trace_id, ts FROM audit_log WHERE trace_id = ? ORDER BY id ASC LIMIT ?";
          args = [tid, limit];
        } else {
          sql = "SELECT id, action, dataset_id, meta, trace_id, ts FROM audit_log WHERE ts >= ? ORDER BY id DESC LIMIT ?";
          args = [since, limit];
        }
        const r = await env.DB.prepare(sql).bind(...args).all();
        return json(r.results || [], 200, { "X-Trace-Id": traceId });
      }

      return json({ error: "not found", path }, 404);
    } catch (e) {
      await bumpMetric(env, ctx, "req:error");
      return json({ error: e.message, stack: (e.stack || "").split("\n")[0] }, 500);
    } finally {
      const dt = Date.now() - t0;
      ctx.waitUntil(
        env.DB.prepare(
          "INSERT INTO metrics (key, n) VALUES ('latency_ms_sum', ?) ON CONFLICT(key) DO UPDATE SET n = n + ?"
        ).bind(dt, dt).run().catch(() => {})
      );
    }
  },

  // ── Cron handler ─────────────────────────────────────────────────────────
  // */5 (every 5 min)  → ping each Space /health
  // */15 (every 15)    → end-to-end canary
  async scheduled(event, env, ctx) {
    if (event.cron === "*/15 * * * *") {
      await runCanary(env, ctx);
      return;
    }
    // Default: */5 health probes
    await runSpaceHealthProbes(env, ctx);
  },

  // ── Queue consumer ──────────────────────────────────────────────────────
  async queue(batch, env, ctx) {
    for (const msg of batch.messages) {
      try {
        ctx.waitUntil(
          env.DB.prepare(
            "INSERT INTO audit_log (action, dataset_id, meta, trace_id, ts) VALUES ('queue_consume', ?1, ?2, ?3, unixepoch())"
          ).bind(
            msg.body?.dataset_id || null,
            JSON.stringify(msg.body || {}).slice(0, 2000),
            msg.body?.trace_id || null
          ).run().catch(() => {})
        );
        msg.ack();
      } catch (e) {
        msg.retry();
      }
    }
  },
};

// ── Cron task implementations ──────────────────────────────────────────────
async function runSpaceHealthProbes(env, ctx) {
  const ua = "Mozilla/5.0 (compatible; SurrogateCursorWorker/1.0; +https://surrogate-1-cursor.ashira.workers.dev)";
  for (const sp of SPACES) {
    const sub = sp.replace("/", "-");
    const t0 = Date.now();
    let code = 0;
    try {
      const resp = await fetch(`https://${sub}.hf.space/health`, {
        headers: { "User-Agent": ua },
        signal: AbortSignal.timeout(15000),
      });
      code = resp.status;
    } catch (e) {
      code = 0;
    }
    const dt = Date.now() - t0;
    ctx.waitUntil(
      env.DB.prepare(
        "INSERT INTO space_health (space_id, http_code, latency_ms, ts) VALUES (?1, ?2, ?3, unixepoch())"
      ).bind(sp, code, dt).run().catch(() => {})
    );
  }
  ctx.waitUntil(
    env.DB.prepare(
      "DELETE FROM space_health WHERE id NOT IN (SELECT id FROM space_health ORDER BY id DESC LIMIT 1000)"
    ).run().catch(() => {})
  );
  // Bust the cached space_health snapshot for /dash and /status
  ctx.waitUntil(env.CACHE.delete("dash:spaces"));
}

// #36 — synthetic canary
async function runCanary(env, ctx) {
  const t0 = Date.now();
  const traceId = newTraceId();
  const slug = "_canary_e2e";
  const auth = env.AUTH_TOKEN || "";
  const base = "https://surrogate-1-cursor.ashira.workers.dev";
  const headers = {
    "Content-Type": "application/json",
    "X-Auth-Token": auth,
    "X-Trace-Id": traceId,
  };
  const errs = [];
  let success = false;
  try {
    // 1. read cursor
    const r1 = await fetch(`${base}/cursor/${slug}`, { headers: { "X-Trace-Id": traceId } });
    if (!r1.ok) errs.push(`read:${r1.status}`);
    const cur = await r1.json().catch(() => ({}));
    // 2. advance
    const r2 = await fetch(`${base}/cursor/${slug}/advance`, {
      method: "POST",
      headers,
      body: JSON.stringify({ size: 1, last_batch: `canary-${Date.now()}` }),
    });
    if (!r2.ok) errs.push(`advance:${r2.status}`);
    // 3. read-back
    const r3 = await fetch(`${base}/cursor/${slug}`, { headers: { "X-Trace-Id": traceId } });
    if (!r3.ok) errs.push(`readback:${r3.status}`);
    const cur2 = await r3.json().catch(() => ({}));
    if (cur2.offset == null || cur.offset == null || cur2.offset <= cur.offset) {
      errs.push(`offset_no_advance:${cur.offset}->${cur2.offset}`);
    }
    // 4. audit lookup by trace_id
    const r4 = await fetch(`${base}/audit?trace_id=${traceId}`, { headers: { "X-Auth-Token": auth } });
    if (!r4.ok) errs.push(`audit:${r4.status}`);
    const auditRows = await r4.json().catch(() => []);
    if (!Array.isArray(auditRows) || auditRows.length < 1) errs.push(`audit_missing:${auditRows?.length || 0}`);
    success = errs.length === 0;
  } catch (e) {
    errs.push(`exception:${e.message?.slice(0, 80) || "?"}`);
  }
  const dt = Date.now() - t0;
  ctx.waitUntil(
    env.DB.prepare(
      "INSERT INTO canary_runs (trace_id, success, latency_ms, errors, ts) VALUES (?1, ?2, ?3, ?4, unixepoch())"
    ).bind(traceId, success ? 1 : 0, dt, errs.join(",").slice(0, 500)).run().catch(() => {})
  );
  ctx.waitUntil(
    env.DB.prepare(
      "DELETE FROM canary_runs WHERE id NOT IN (SELECT id FROM canary_runs ORDER BY id DESC LIMIT 500)"
    ).run().catch(() => {})
  );
  ctx.waitUntil(bumpMetric(env, ctx, success ? "canary:ok" : "canary:fail"));
}
