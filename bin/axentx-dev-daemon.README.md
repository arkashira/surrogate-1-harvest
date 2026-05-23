# axentx-dev-daemon

> ROADMAP-100 #89. The work producer — generates dev tasks for the rotation.

| Field | Value |
|---|---|
| Role | Picks next project × focus, calls dev-role LLM, writes implementation to review-queue |
| Stage in pipeline | (prd-tasks or rotation) → **dev** → reviewer |
| In | `dev-queue/*.json` (PRD tasks) + rotation generator (when queue empty) |
| Out | `review-queue/*.json` |
| Idempotency | Each item carries `task_id`; existing review-queue file with same id is overwritten only on retry |
| Concurrency | **dev × 6** — one per axentx project. Project pinned by `DEV_PROJECT` env. |

## Env

| Var | Default | What |
|---|---|---|
| `DEV_POLL_SEC` | `60` | Poll cycle |
| `DEV_PROJECT` | rotation | Pin to one of `Costinel/vanguard/airship/axiomops/workio/surrogate-1` |
| `AXENTX_ROOT` | `/opt/axentx` | Where the per-project clones live |

Rotation focus: `discovery → design → backend → frontend → quality → ops` per project per cycle.

## System prompt summary

"Senior engineer. Take task, output: file list + diff. Diff stays under 250 LOC (ROADMAP #7). For `*-core` packages with `TEST_FIRST_PROJECTS` set, write tests first then impl (ROADMAP #5). Use idiomatic patterns of the target project — read existing files first."

## Failure modes

- Diff > 250 LOC → auto-split into N sequential items.
- Lint fail → loop back to dev (ROADMAP #6).
- LLM ladder exhausted → re-queue with backoff.
