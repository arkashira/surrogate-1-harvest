# axentx-prd-daemon

> ROADMAP-100 #89. Terminal product-discovery stage — PRD + epics + dev tasks.

| Field | Value |
|---|---|
| Role | Turns full opportunity dossier into PRD + epics + concrete dev tasks |
| Stage in pipeline | marketing → **prd** → dev (or → architect first if NEW-PRODUCT) |
| In | `prd-queue/*.json` |
| Out | `dev-queue/*.json` (one per task) + decision file in `agent-decisions/` |
| Idempotency | Tasks individually keyed; re-runs dedupe by `(prd_id, task_id)` |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `PRD_POLL_SEC` | `90` | Poll cycle |

## System prompt summary

"PRD with: problem, goals, non-goals, user stories (`As a … I want … so that …`), acceptance criteria, KPIs, kill criteria (3 measurable abandon-if conditions, ROADMAP #25), MVP scope. Then break into epics → 250-LOC-or-less stories. Output strict JSON — every story becomes a dev-queue item."

## Failure modes

- For NEW-PRODUCT: requires architect output in item.history; if absent, route to architect first.
- Story too large → auto-split (ROADMAP #7 cap = 250 LOC).
