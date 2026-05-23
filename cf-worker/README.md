# CF Worker — surrogate-1-cursor

Replaces the HF Space's `/cursor/*` + `/dynamic-datasets` + `/health` endpoints.
Why: CPU-Basic Space kept hitting 16 GB OOM cap with only the cursor server
running — Cloudflare Worker is free + 0ms cold start + atomic D1 ops.

## URLs

```
PROD:  https://surrogate-1-cursor.ashira.workers.dev
```

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/health` | GET | — | `{status:"ok", ts:...}` |
| `/dynamic-datasets` | GET | — | `[{slug, id, schema, score, cap, ...}]` (KV-cached 60s) |
| `/cursor/<slug>` | GET | — | `{dataset_id, offset, total, last_batch, updated_at}` |
| `/cursor/<slug>/advance` | POST | `{size: 1000, last_batch?: "..."}` | new cursor row after atomic increment |
| `/datasets` | POST | `{slug, hf_id, schema?, score?, cap?}` | `{ok, slug}` |

## Deploy

```bash
# via Cloudflare API (no wrangler needed)
curl -X PUT \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -F 'metadata=@metadata.json;type=application/json' \
  -F 'worker.js=@worker.js;type=application/javascript+module' \
  https://api.cloudflare.com/client/v4/accounts/$CF_ACCT/workers/scripts/surrogate-1-cursor

# or with wrangler (cleaner)
npx wrangler deploy
```

`metadata.json` for API path:
```json
{
  "main_module": "worker.js",
  "compatibility_date": "2026-01-01",
  "bindings": [
    {"type": "d1", "name": "DB", "id": "ae95ac58-7b7e-40d9-8708-518c23281ae6"},
    {"type": "kv_namespace", "name": "CACHE", "namespace_id": "3e334007e0cd472493616f8c67337270"}
  ]
}
```

## Free tier capacity

- Workers: 100k req/day (we run ~10k, well within)
- D1: 5M reads/day, 100k writes/day (each cursor advance = 1 write; ~1k/day)
- KV: 100k reads/day, 1k writes/day (60s cache on /dynamic-datasets means ~1.5k reads/day for the only write)

= comfortably free at current harvest volume.

## Datasets seeded (2026-05-02 from HF mining)

11 high-impact datasets selected via HF API trending (sort by likes×downloads).
See `../docs/research/2026-05-02-hf-mining-improvements.md` for selection rationale.
