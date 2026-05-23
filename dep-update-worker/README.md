# dep-update-worker

> ROADMAP-100 #19. Self-hosted dependency-update PR bot. Replaces
> Renovate / Dependabot — no GitHub App quota, single Worker, weekly cron.

## What it does

Every Monday 03:30 UTC (`triggers.crons` in `wrangler.toml`), walks each
configured repo, reads `requirements.txt` / `package.json`, looks up
the latest pinned version on PyPI / npm, and opens **one batched PR per
repo** with all updates as a single commit.

Manual trigger: `GET /run` (all repos) or `GET /run?repo=axentx/foo`.

## Setup

```bash
cd dep-update-worker
wrangler deploy
wrangler secret put GH_PAT   # fine-grained PAT, contents:write + pull_requests:write
```

Required PAT scopes per target repo:

- `Contents: Read and write`
- `Pull requests: Read and write`
- `Metadata: Read-only`

## Configure target repos

Edit `wrangler.toml` `vars.REPOS` (JSON array). Default list covers the
6 axentx repos plus the harvest itself.

To dry-run without opening PRs: `wrangler.toml` set `DRY_RUN = "1"`.

## Limitations

- Bumps `==` pins only (Python). `>=` / `~=` left alone.
- npm: only top-level `dependencies` / `devDependencies`. Lockfile
  regeneration left to CI.
- No security-prioritization (use the `cve-monitor.yml` workflow for that).
- One PR per repo per week. If you want more cadence, edit the cron.
