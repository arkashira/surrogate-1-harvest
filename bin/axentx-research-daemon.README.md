# axentx-research-daemon

> ROADMAP-100 #89. Pain-point miner.

| Field | Value |
|---|---|
| Role | Mines real-world dev/SaaS pain points from public sources |
| Stage in pipeline | Producer (no upstream queue) |
| In | (external) Reddit `/r/programming`, `/r/SaaS`, `/r/devops`, `/r/sysadmin`, HN front, dev.to top |
| Out | `state/swarm-shared/research-queue/*.json` |
| Idempotency | Per-source URL + post-id hashed; deduped against last 30 days |
| Concurrency | Multi-instance OK — set `RESEARCH_WORKER_ID=<int>` to shard sources |

## Env

| Var | Default | What |
|---|---|---|
| `RESEARCH_POLL_SEC` | `600` | Poll cycle, 10 min |
| `RESEARCH_WORKER_ID` | `0` | Shard id when running >1 instance |
| `OPENROUTER_API_KEY`, `GROQ_API_KEY`, … | — | LLM ladder (any one suffices) |
| `REPO_ROOT` | `/opt/surrogate-1-harvest` | Where queues live |

## System prompt summary

"Market researcher. For each post, output one JSON line: `{title, pain_one_liner, root_pain, audience, severity, frequency, source_url}`. Skip support/feature requests/already-solved. Focus on dev tools, infra, observability, security, automation."

## Failure modes

- Source HTML changes → soft-fail item, log, continue.
- LLM ladder exhausted → backoff `POLL_SEC × 2` then resume.
- Queue write fail → atomic rename, retry next cycle.
