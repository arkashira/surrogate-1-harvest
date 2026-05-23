# axentx-ux-daemon

> ROADMAP-100 #89. UX design pass before PRD split.

| Field | Value |
|---|---|
| Role | Drafts user flows + wireframe descriptions + error/edge states |
| Stage in pipeline | marketing → **ux** → prd |
| In | `marketing-queue/*.json` (after BMC + GTM done) |
| Out | `prd-queue/*.json` with `ux` annotations attached |
| Idempotency | Per-UUID; UX block stored in item.history |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `UX_POLL_SEC` | `120` | Poll cycle |

## System prompt summary

"Senior UX designer. For each validated product/feature, output strict JSON: `{user_flows: [{name, steps, happy_path}], wireframes: [{screen, layout}], error_states, edge_cases}`. Layout described in plain text (no images)."

## Failure modes

- Non-JSON output → re-prompt once, else advance with `ux:{}` placeholder.
- LLM exhaustion → re-queue.
