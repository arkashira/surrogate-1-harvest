# axentx-perf-daemon

> ROADMAP-100 #89. Performance / scalability review pass.

| Field | Value |
|---|---|
| Role | Catches N+1, unbounded queries, missing indexes, sync-in-async, leaks |
| Stage in pipeline | qa → **perf** (parallel with security) → commit |
| In | `qa-queue/*.json` |
| Out | `commit-queue/*.json` (OK) or `dev-queue/*.json` (PERF-BLOCK with findings) |
| Idempotency | Adds `perf_findings` block to item.history |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `PERF_POLL_SEC` | `60` | Poll cycle |

## System prompt summary

"Performance engineer. PERF-BLOCK only on HIGH (would cause prod outage at any reasonable scale). Med = note in commit, OK proceed. Low = acceptable."

## Failure modes

- Hot-path identification fails → OK + note.
- LLM exhaustion → re-queue.
