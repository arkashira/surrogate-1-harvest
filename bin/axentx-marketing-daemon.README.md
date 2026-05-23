# axentx-marketing-daemon

> ROADMAP-100 #89. Positioning + competitor map + GTM.

| Field | Value |
|---|---|
| Role | Drafts positioning, competitor scan, go-to-market for BUILD-tagged items |
| Stage in pipeline | business → **marketing** → prd |
| In | `marketing-queue/*.json` |
| Out | `prd-queue/*.json` |
| Idempotency | Positioning + competitor map saved in item.history; never re-run for same UUID |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `MARKETING_POLL_SEC` | `90` | Poll cycle |

## System prompt summary

"Head of product marketing. Output JSON: positioning statement, ICP, top-3 competitors with differentiator, channels (organic/paid/community/partnership), launch sequence (waitlist → beta → GA), success metrics (activation/retention/revenue)."

## Failure modes

- Competitor data thin → flag `low_confidence:true`, still advance.
- LLM exhaustion → re-queue.
