# axentx-design-thinking-daemon

> ROADMAP-100 #89. Idea validation — Lean Canvas + 5-Whys.

| Field | Value |
|---|---|
| Role | Validates BD-routed pains with two frameworks back-to-back |
| Stage in pipeline | bd → **design-thinking** → business |
| In | `design-queue/*.json` |
| Out | `business-queue/*.json` (PROCEED) or `done/*.json` (REJECT) |
| Idempotency | UUID preserved; rejection reason + framework outputs persisted in item history |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `DESIGN_POLL_SEC` | `60` | Poll cycle |

## System prompt summary

"Two passes: (1) Lean Canvas — problem, segments, UVP, channels, revenue; (2) 5-Whys to surface root pain. PROCEED only if both pass coherence check. Bias toward REJECT for thin signal."

## Failure modes

- Either pass yields ambiguous output → REJECT, log to `done`.
- LLM ladder exhausted → re-queue with backoff.
