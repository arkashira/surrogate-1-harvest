# axentx-qa-daemon

> ROADMAP-100 #89. TDD test plan + PASS/BLOCK gate.

| Field | Value |
|---|---|
| Role | Writes test plan; PASS or BLOCK depending on coverage adequacy |
| Stage in pipeline | reviewer → **qa** → security + perf (parallel) |
| In | `qa-queue/*.json` |
| Out | `security-queue/*.json` + `perf-queue/*.json` (PASS) or `dev-queue/*.json` (BLOCK) |
| Idempotency | Per-UUID; first-line PASS:/BLOCK: prefix is the contract |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `QA_POLL_SEC` | `30` | Poll cycle |

## System prompt summary

"QA engineer. For each approved change: TDD test plan with sections (unit / integration / edge cases / regression). First line MUST be `PASS:` or `BLOCK: <reason>`."

## Failure modes

- LLM omits PASS/BLOCK marker → re-prompt once, else BLOCK with diagnostic.
- LLM exhaustion → re-queue.
