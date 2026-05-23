# axentx-docs-daemon

> ROADMAP-100 #89. Auto-updates README + CHANGELOG.

| Field | Value |
|---|---|
| Role | Drafts README/CHANGELOG patches when public surface changes |
| Stage in pipeline | commit → **docs** → release |
| In | `done/*.json` items where `stage=='commit'` and touched files imply doc-relevant change |
| Out | `commit-queue/*.json` (a docs-only commit, no review needed) |
| Idempotency | Cursor file `state/.docs-daemon-cursor.json` |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `DOCS_POLL_SEC` | `300` | 5 min poll |

## System prompt summary

"Technical writer. For each shipped commit: propose README + CHANGELOG patch. Set `needs_update=false` if commit is internal-only (test, refactor, chore). Output strict JSON with `patches: {readme:..., changelog:...}` or `{needs_update:false}`."

## Failure modes

- Patch doesn't apply cleanly → re-queue once, then drop with diag.
- Cursor lost → resume from latest item, accept some duplication.
