# axentx Self-Serve API — Route Surface (Design Doc)

**Status**: design / pre-implementation. Endpoints listed below are not
live yet — they describe the intended public surface for ROADMAP-100 #91.

**Goal**: turn the existing cursor + agent pipeline into a metered API
that external callers can pay to use. Free tier: 100 requests/day per
API key. Paid: $0.01/call after the free quota.

## Authentication

All endpoints require an API key in either header:

```
X-API-Key: ax_live_<32-char-token>
Authorization: Bearer ax_live_<32-char-token>
```

Keys are issued from `/v1/keys/issue` (admin-only, internal). Each key
is associated with a `customer_id` and a `tier` (`free` | `pro` |
`enterprise`).

## Pricing (target)

| tier        | monthly base | included calls | overage           |
|-------------|--------------|----------------|-------------------|
| free        | $0           | 100/day         | 429 throttled     |
| pro         | $49          | 50,000/month    | $0.01/call        |
| enterprise  | custom       | committed       | committed         |

Per-call attribution: every billable call writes a row into D1
`api_usage(customer_id, ts, endpoint, request_units, cost_micros)`.

Stripe is the system of record for invoicing. Worker calls Stripe
Meter Events on each billable call and reconciles nightly.

## Routes

### `POST /v1/discover`

Run one cycle of the product-discovery pipeline (research → bd → design
→ business → marketing → prd) on a topic the caller supplies. Returns
the structured PRD.

**Request body**

```json
{
  "topic": "AI-assisted code review for small teams",
  "audience": "engineering managers at startups (2-20 engineers)",
  "constraints": ["max 14 days to v0.1", "free-tier infra preferred"],
  "depth": "quick" 
}
```

`depth`: `quick` (≤30s, single-pass) | `deep` (≤4min, 3-stage fanout).

**Response 200**

```json
{
  "discovery_id": "dsc_018f3e5e7a",
  "prd": {
    "problem": "...",
    "audience": "...",
    "kill_criteria": ["...", "...", "..."],
    "scope_v0_1": "...",
    "acceptance_criteria": ["...", "..."],
    "north_star_metric": "...",
    "open_questions": ["..."]
  },
  "evidence_ids": ["ev_018f3e5e91", "ev_018f3e5e92"],
  "elapsed_ms": 3420,
  "credits_used": 1
}
```

**Errors**

- `429 too_many_requests` — daily/monthly quota exceeded.
- `402 payment_required` — paid tier billing past due.
- `503 upstream_unavailable` — provider chain exhausted.

### `POST /v1/architect`

Given a PRD or product brief, return an Architecture Decision Record
(ADR) with stack picks + folder layout + first-release scope.

**Request body**

```json
{
  "discovery_id": "dsc_018f3e5e7a",
  "prefer": ["typescript", "cloudflare-workers"],
  "infra_constraints": "free-tier only"
}
```

A `discovery_id` is preferred; alternatively the caller can pass a raw
`{ "brief": "..." }` to skip the discovery stage.

**Response 200**

```json
{
  "adr_id": "adr_018f3e5fa1",
  "tech_stack": {
    "backend": "Hono on Cloudflare Workers",
    "frontend": "Next.js on CF Pages",
    "datastore": "D1 + KV (cursor) + Vectorize (embeddings)",
    "queue": "CF Queues",
    "cache": "KV"
  },
  "folder_layout": ["src/api/", "src/lib/", "tests/", "schema/"],
  "data_model": [{"entity": "User", "fields": [...] }],
  "deployment": "cf workers + pages",
  "third_party": ["stripe@2024 — billing"],
  "open_questions": ["..."],
  "first_release_scope": "...",
  "credits_used": 2
}
```

`/v1/architect` costs 2 credits because it dispatches the architect
daemon (heavier prompt + JSON validation pass).

### `GET /v1/keys/usage`

Returns the caller's quota state for the current period.

```json
{
  "tier": "free",
  "period_start": "2026-05-01T00:00:00Z",
  "period_end": "2026-05-01T23:59:59Z",
  "calls_used": 47,
  "calls_included": 100,
  "calls_remaining": 53,
  "overage_credits": 0
}
```

### `POST /v1/keys/issue` (admin only)

Issues a new key for a customer. Internal — fronted by an additional
`X-Admin-Token` header. Will eventually be exposed via the dashboard.

## Throttling + abuse

- Per-key: rate-limit binding 60 req/min (overridable per-tier).
- Per-IP fallback: 200 req/hour for unauthenticated calls (returns
  401 anyway, but cheap to limit before signature check).
- Free-tier abuse: when daily quota hits, instead of `429` we return
  a clear upgrade nudge in the body.

## Audit

Every billable call writes to D1 `api_audit(call_id, customer_id, ts,
endpoint, request_hash, response_hash, latency_ms, status_code)` so
disputes can be reconciled against Stripe Meter Events.

## Open design questions

1. **Idempotency**: should we accept `Idempotency-Key` header on
   `/v1/discover` to dedupe retries? Cost: extra KV write per call.
2. **Streaming**: `/v1/discover?stream=1` could push intermediate
   stages over SSE. Useful for `depth=deep` (long-running). Defer
   until measured demand.
3. **Webhooks**: callers may want a webhook on `discovery.completed`
   instead of polling. Add `webhook_url` to request body — POST signed
   event when done. Defer to v2.
4. **Custom fine-tunes**: enterprise tier could let callers attach
   their own LoRA adapter id and have the architect daemon route
   through it. Pricing: $X/run-hour. Defer to v3 — depends on adapter
   eval gate (#2) being mature.

## Dependencies (from ROADMAP-100)

- #76 cursor service rate-limit per IP — must ship first; throttling
  primitives are reused here.
- #91 metered billing — this doc *is* the surface for #91.
- #94 ToS + Privacy Policy — required before taking payment.

## Non-billable infra calls

- `GET /healthz` — public; returns 200 if Worker can reach D1.
- `GET /metrics` — Prometheus-format metrics for Grafana scraping;
  protected by `X-Admin-Token`.

These never count against the caller's quota.
