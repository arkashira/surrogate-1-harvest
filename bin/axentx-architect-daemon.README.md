# axentx-architect-daemon

> ROADMAP-100 #89. Architecture Decision Record drafter.

| Field | Value |
|---|---|
| Role | Designs ADR + tech stack + folder layout + data model for NEW-PRODUCT items |
| Stage in pipeline | prd → **architect** → prd (loops back) |
| In | `prd-queue/*.json` filtered to `bd_verdict.verdict == "NEW-PRODUCT"` |
| Out | Same `prd-queue` item with `architecture` field populated |
| Idempotency | Per-UUID; re-runs replace `architecture` block atomically |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `ARCH_POLL_SEC` | `120` | Poll cycle |

## System prompt summary

"Principal engineer drafting an ADR for a brand-new product. Output strict JSON: tech stack, top-level folders, data model, public surface, deploy target, scaling plan, security/auth model, observability, cost ceiling. Justify each choice in one line."

## Failure modes

- LLM produces non-JSON → reject + retry (max 2) → SEC-BLOCK back to prd with note.
- Cost ceiling missing → re-prompt forcing it.
