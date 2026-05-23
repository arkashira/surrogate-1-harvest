# axentx-release-daemon

> ROADMAP-100 #89. Daily semver tags + GitHub releases.

| Field | Value |
|---|---|
| Role | Walks each axentx repo, tags semver, drafts release notes, `gh release create` |
| Stage in pipeline | docs → **release** → content |
| In | git history of each `axentx/<project>` repo (last-tag → HEAD) |
| Out | git tag + GitHub Release per project |
| Idempotency | Idempotent on tag name; existing tag = skip |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `RELEASE_POLL_SEC` | `86400` | 24 h poll |
| `AXENTX_ROOT` | `/opt/axentx` | Per-project clones |
| Threshold | ≥5 commits since last tag, else skip |

## System prompt summary

"Given commit subjects since last tag, decide semver bump (major / minor / patch) + draft release notes. Strict JSON. Rules: ANY breaking change → major. New feature commits → minor. Else patch."

## Failure modes

- gh release create fails (auth/rate) → log, skip, retry next cycle.
- Repo lacks any prior tag → bootstrap with `v0.1.0`.
