# axentx-trends-daemon

> ROADMAP-100 #89. Opportunity scanner — trending tech.

| Field | Value |
|---|---|
| Role | Discovers hot dev/infra/AI projects from trending feeds |
| Stage in pipeline | Producer (no upstream queue) |
| In | (external) `github.com/trending`, HN `/show` + `/front` last week, `dev.to/top`, ProductHunt RSS |
| Out | `research-queue/*.json` (treated as opportunity-flavored pain) |
| Idempotency | Cursor file `state/.trends-cursor.json` tracks already-seen items |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `TRENDS_POLL_SEC` | `21600` | 6 h poll cycle |

## System prompt summary

"Scan trending items. NDJSON output: `{name, one_liner, problem, emerging_signal, relevance_to_axentx, why}`. Skip games/hardware/crypto/consumer apps. Focus on dev tools, SaaS infra, AI, observability, security, automation, productivity."

## Failure modes

- 429 from GH trending → silent skip cycle.
- Empty result → no-op (not an error).
- LLM extract fails → drop the source's batch, keep cursor at previous mark.
