# axentx-bd-daemon

> ROADMAP-100 #89. Business Development triage.

| Field | Value |
|---|---|
| Role | Classifies pains/opportunities into PASS / EXTEND `<project>` / NEW-PRODUCT |
| Stage in pipeline | research → **bd** → design |
| In | `research-queue/*.json` |
| Out | `design-queue/*.json` (proceed) or `done/*.json` (PASS) |
| Idempotency | Item UUID preserved across stages; `agent_decisions` row written once per stage |
| Concurrency | Single instance per BD_BUDGET |

## Env

| Var | Default | What |
|---|---|---|
| `BD_POLL_SEC` | `60` | Poll cycle |
| `BD_BUDGET` | `500` | Max items/day to claim |

## System prompt summary

"Pain triage. Active portfolio: Costinel/vanguard/airship/workio/axiomops/surrogate. Verdicts: `EXTEND <project>` (fits as feature), `NEW-PRODUCT` (fresh fit), `PASS` (off-strategy, e.g. consumer/gaming/hardware). BD only routes — no funding/build call here."

## Failure modes

- Parse fail → `fail(item, "bd", reason)` → `done` with error tag.
- LLM ladder exhausted → re-queue.
