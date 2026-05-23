# axentx-reviewer-daemon

> ROADMAP-100 #89. Code review — APPROVE / REJECT.

| Field | Value |
|---|---|
| Role | Pragmatic principal-engineer review with dynamic threshold |
| Stage in pipeline | dev → **reviewer** → qa |
| In | `review-queue/*.json` |
| Out | `qa-queue/*.json` (APPROVE) or `dev-queue/*.json` (REJECT, with findings) |
| Idempotency | Per-UUID; rubric version pinned per verdict (ROADMAP #4) |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `REVIEW_POLL_SEC` | `30` | Poll cycle |

## System prompt summary

"Default APPROVE; reject only for clear correctness/security/data bugs or stack-mismatched reviews (use CODEOWNERS to pick rubric). Never reject on style/naming alone. Rubric version stamped on every verdict."

## Failure modes

- Re-reject loop > 3 → escalate to human (Discord ping `@arkashira`).
- LLM exhaustion → re-queue.
