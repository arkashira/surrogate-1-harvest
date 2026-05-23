# axentx-business-daemon

> ROADMAP-100 #89. Business Model Canvas + market sizing + pricing.

| Field | Value |
|---|---|
| Role | Builds full BMC and judges BUILD vs NO-GO |
| Stage in pipeline | design-thinking → **business** → marketing |
| In | `business-queue/*.json` |
| Out | `marketing-queue/*.json` (BUILD) or `done/*.json` (NO-GO) |
| Idempotency | BMC + sizing + pricing snapshot stored in item.history |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `BUSINESS_POLL_SEC` | `90` | Poll cycle |

## System prompt summary

"BMC: customer segments, value props, channels, customer relations, revenue streams, key resources, key activities, partners, cost structure. Plus market sizing (TAM/SAM/SOM, conservative) and starter pricing model. BUILD only when sizing × margin × strategic-fit clears thresholds; NO-GO with specific reason otherwise."

## Failure modes

- Numeric reasoning fail → NO-GO with diag note.
- LLM exhaustion → re-queue.
