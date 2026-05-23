# axentx-customer-poll-daemon

> ROADMAP-100 #89. Weekly Discord poll for product validation (ROADMAP #23).

| Field | Value |
|---|---|
| Role | Generates 3 Discord polls/week to validate top BUILD opportunity |
| Stage in pipeline | Side-channel (reads done items, posts to Discord) |
| In | `done/*.json` items with `business_verdict.verdict == "BUILD"` from last 7 days |
| Out | Discord webhook post + D1 cursor record |
| Idempotency | Skips if a poll for the same opportunity_id already posted this week |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `CUSTOMER_POLL_SEC` | `604800` | 7-day cycle |
| `DISCORD_WEBHOOK` | — | Where polls land |

## System prompt summary

"Generate 3 Discord poll questions (each ≤140 chars) that ask about the user's actual behavior (not opinion). Output JSON: `{questions, options_per_q}`. Default options: yes/no/maybe."

## Failure modes

- No webhook configured → no-op.
- No BUILD opportunity in window → no-op.
- LLM exhaustion → retry next cycle.
