# axentx-commit-daemon

> ROADMAP-100 #89. Writes the commit + pushes to `axentx/<project>`.

| Field | Value |
|---|---|
| Role | Stages files from item.diff, writes Conventional Commit, `git push` |
| Stage in pipeline | (security ∩ perf) → **commit** → docs |
| In | `commit-queue/*.json` |
| Out | `done/*.json` + git push to `axentx/<project>:main` |
| Idempotency | Commit hash recorded; rerun is a no-op |
| Concurrency | Single instance per project (file lock on `<project>-commit.lock`) |

## Env

| Var | Default | What |
|---|---|---|
| `COMMIT_POLL_SEC` | `60` | Poll cycle |
| `AXENTX_ROOT` | `/opt/axentx` | Where the per-project clones live |

## System prompt summary

(No LLM prompt — pure mechanical) Picks item, applies diff to working tree, runs lint+typecheck (ROADMAP #6), writes Conventional Commit message from PRD task title, `git push origin main`.

## Failure modes

- Lint fail → re-queue to `dev` with stderr attached.
- Push reject (branch protection #3) → open PR via gh CLI instead.
- Conflict → fail item with diff dump.
