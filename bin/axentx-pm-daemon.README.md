# axentx-pm-daemon

> ROADMAP-100 #89. Sprint state machine + ceremonies.

| Field | Value |
|---|---|
| Role | Tracks sprint number + boundaries, runs planning/standup/retro prompts |
| Stage in pipeline | Side-channel (writes to `state/axentx-pm-state.json`) |
| In | `state/axentx-pm-state.json` (own state) + `done/*.json` (signal) |
| Out | `state/axentx-pm-state.json` (updated) + Discord notifications on boundary |
| Idempotency | State file is the source of truth; restarts replay safely |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `PM_POLL_SEC` | `60` | Poll cycle |

## System prompt summary

"PM running ceremonies for the axentx product family. Concise. Actionable bullets, no fluff."

## Sprint boundaries

- 2-week iterations.
- On boundary: prompt for retro + plan next sprint, post to Discord.

## Failure modes

- Discord webhook missing → state still advances.
- LLM exhaustion → re-queue.
