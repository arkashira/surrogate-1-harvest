# axentx-content-daemon

> ROADMAP-100 #89. Blog post + social copy generator.

| Field | Value |
|---|---|
| Role | Drafts dev-marketing copy from shipped commits |
| Stage in pipeline | release → **content** (terminal) |
| In | git log over last `CONTENT_POLL_SEC` per project |
| Out | `docs/blog/<date>/<slug>.md` in target project repo, pushed via commit-queue |
| Idempotency | Slug = sha-first-12 of commit batch; same batch → no-op |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `CONTENT_POLL_SEC` | `14400` | 4 h cycle |
| `AXENTX_ROOT` | `/opt/axentx` | Per-project clones |
| Projects | `Costinel/vanguard/airship/axiomops/workio/surrogate-1` |

## System prompt summary

"Developer-marketing writer. Voice: technical, no fluff, link-baity title fine but body must deliver. Output: blog post + LinkedIn copy + tweet thread. Strict markdown."

## Failure modes

- Empty commit window → no-op.
- LLM ladder exhausted → re-queue.
