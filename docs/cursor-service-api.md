# Cursor service — API reference

> ROADMAP-100 #86. The Cloudflare Worker that owns dataset cursors,
> audit log, metrics, and the dashboard. Single deploy:
> [https://surrogate-1-cursor.ashira.workers.dev](https://surrogate-1-cursor.ashira.workers.dev)
>
> Source: [`cf-worker/worker.js`](../cf-worker/worker.js).
> Schema: [`cf-worker/schema.sql`](../cf-worker/schema.sql).

## Auth

Mutating routes require an `X-Auth-Token` header that matches the Worker
secret `AUTH_TOKEN` (set via `wrangler secret put`). Read-only routes
(`/health`, `/dash`, `/metrics`, `GET /cursor/...`, `GET /dynamic-datasets`)
are open. CORS is `*`.

```bash
curl -H "X-Auth-Token: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"size": 1000}' \
  https://surrogate-1-cursor.ashira.workers.dev/cursor/my-dataset/advance
```

## Endpoints

### `GET /health` · `GET /`

Liveness probe. No auth.

```json
{"status": "ok", "service": "surrogate-1-cursor", "ts": 1714720000000}
```

### `GET /dynamic-datasets`

List of known harvest datasets, ordered by score desc, capped 5000.
Cached 60s in KV.

```bash
curl -s https://surrogate-1-cursor.ashira.workers.dev/dynamic-datasets | jq '.[0]'
```

```json
{"slug":"reddit-pains-v3","id":"ashirato/reddit-pains-v3",
 "schema":"messages","license":"mit","cap":50000,
 "score":0.91,"downloads":1284,"discovered_ts":1714000000}
```

### `GET /cursor/<slug>`

Read cursor state for a dataset. No auth.

```json
{"dataset_id": "reddit-pains-v3",
 "offset": 12000, "total": 50000, "exhausted": 0,
 "last_batch": "<opaque cursor token from caller>",
 "updated_at": 1714720000}
```

If the slug has never been advanced, returns the same shape with
`offset: 0, total: null, exhausted: 0, updated_at: null`.

### `POST /cursor/<slug>/advance`

Atomically advance the cursor. Auth required.

Request body:

| Field | Type | Default | Notes |
|---|---|---|---|
| `size` | integer | 1000 | 1 ≤ size ≤ 100000 (rows consumed in this batch) |
| `last_batch` | string | "" | Opaque caller-provided checkpoint, ≤ 200 chars |
| `total` | integer | null | Sets total once known; null = leave unchanged |
| `exhausted` | bool | false | `true` finalizes the cursor |

```bash
curl -H "X-Auth-Token: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"size": 1000, "last_batch": "page=12", "total": 50000}' \
  https://surrogate-1-cursor.ashira.workers.dev/cursor/reddit-pains-v3/advance
```

Response (after-state):
```json
{"dataset_id":"reddit-pains-v3","offset":13000,
 "total":50000,"last_batch":"page=12","exhausted":0,
 "updated_at":1714720100}
```

When `offset >= total`, `exhausted` flips to `1` server-side.

### `POST /datasets`

Register or upsert a dataset. Auth required.

```json
{"slug": "reddit-pains-v4",
 "hf_id": "ashirato/reddit-pains-v4",
 "schema": "messages",
 "license": "mit",
 "cap": 50000,
 "score": 0.85}
```

`slug` and `hf_id` are required; rest default to `messages` / `null` /
`50000` / `0.5`. Returns `{"ok": true, "slug": "..."}`.

### `GET /audit`

Audit log for any cursor/dataset mutation. Auth required.

| Param | Type | Default |
|---|---|---|
| `limit` | int | 100 (max 500) |
| `since` | int (epoch sec) | 0 |

```bash
curl -H "X-Auth-Token: $AUTH_TOKEN" \
  "https://surrogate-1-cursor.ashira.workers.dev/audit?limit=20"
```

Returns `[{id, action, dataset_id, meta, ts}, …]`.

### `GET /metrics`

Prometheus exposition format. Counters keyed by route. No auth.

```
# HELP surrogate_cursor_requests Total requests by endpoint
# TYPE surrogate_cursor_requests counter
surrogate_cursor_requests{key="req:health"} 12834
surrogate_cursor_requests{key="req:advance"} 421
surrogate_cursor_requests{key="req:cursor_read"} 9210
...
```

### `POST /tasks/push`

Forward an arbitrary JSON payload onto `surrogate-1-tasks` (CF Queues),
the third queue backend. Auth required.

```bash
curl -H "X-Auth-Token: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"kind":"reindex","slug":"reddit-pains-v3"}' \
  https://surrogate-1-cursor.ashira.workers.dev/tasks/push
```

### `POST /ai/<model>`

Proxy to Workers AI — used as the 12th provider in the LLM fallback
ladder. Auth required. URL-encode the model id after `/ai/`.

```bash
curl -H "X-Auth-Token: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":128}' \
  https://surrogate-1-cursor.ashira.workers.dev/ai/llama-3.3-70b-instruct
```

Response is the raw Workers AI shape: `{response: "...", usage: {...}}`
or model-specific.

### `GET /dash`

HTML dashboard. Renders top 50 datasets by score, request counters, last
20 audit entries. Open in browser.

## Error shape

All errors return JSON:

```json
{"error": "<message>", "path": "<request path on 404>"}
```

| HTTP | Cause |
|---|---|
| 400 | Missing/invalid body fields |
| 401 | Missing or wrong `X-Auth-Token` |
| 404 | Unknown route |
| 500 | Server error (logged + counted as `req:error`) |

## Bindings (deployer reference)

`cf-worker/wrangler.toml`:

| Binding | Type | Resource |
|---|---|---|
| `DB`           | D1     | `surrogate-1-cursor` |
| `CACHE`        | KV     | namespace `surrogate-1-cache` |
| `AI`           | AI     | Workers AI |
| `TASKS_QUEUE`  | Queue  | `surrogate-1-tasks` (producer + consumer) |

Cron: `*/5 * * * *` (housekeeping — re-checks dataset scores, prunes
stale audit rows).

## See also

- [Bruno collection](./cursor-service.bruno.json) — pre-baked requests
- [Worker source](../cf-worker/worker.js)
- [D1 schema](../cf-worker/schema.sql)
