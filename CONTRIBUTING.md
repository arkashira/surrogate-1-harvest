# Contributing

> ROADMAP-100 #85. The harvest is mostly bot-driven, but humans (and other
> agents) still need a clear contract. Follow the conventions below.

## TL;DR

1. Branch off `main`. PR back to `main`.
2. Conventional Commits: `<type>(<scope>): <subject>`.
3. Keep diffs ≤ 250 LOC (ROADMAP #7). Bigger? Split.
4. Test before push: `pytest -x -q` (Python) or `wrangler dev` (Worker).
5. Update `docs/` when you change public surface.

## Repository layout

```
.
├── bin/                Daemons + helper scripts (Python + bash)
├── agents/             Per-role prompt assets (JSON/Markdown)
├── cf-worker/          Cloudflare Worker (D1 + KV + AI + Queues)
├── config/, configs/   Static config (LLM ladder, RAG sources)
├── data/               Read-only seed datasets used by daemons
├── docs/               Architecture, runbooks, ROADMAP, this file
├── state/              Local queue files (gitignored)
├── systemd/            Service units for the GCP host
├── Dockerfile          Hermes (HF Space) image
├── start.sh            VM bootstrap
└── requirements.txt    Python deps for all daemons
```

## Commits

Conventional Commits is enforced by `commitlint` on push (see
`.github/workflows/commitlint.yml`).

| Type | When |
|---|---|
| `feat`     | New user-visible capability |
| `fix`      | Bug fix |
| `docs`     | Docs-only change |
| `perf`     | Performance improvement |
| `refactor` | No behavior change |
| `test`     | Tests only |
| `build`    | Build system / Dockerfile / requirements |
| `ci`       | GH workflows |
| `chore`    | Tooling, no impact |
| `agents`   | Daemon prompts / pipeline behavior |
| `infra`    | Worker / Supabase / VM systemd / DNS |
| `data`     | Dataset additions / regenerations |
| `rag`      | Vectorize index, embeddings, knowledge-base |

Subject line ≤ 100 chars. Imperative mood. Body explains *why*, not *what*.

Example:
```
agents(reviewer): drop dynamic threshold floor from 0.55 to 0.45

Reviewer was over-rejecting on small fix-typo PRs since the recent
prompt change. Floor at 0.45 keeps the auto-bot moving without
letting actual blockers slip through.
```

## PR checklist

Before opening:

- [ ] Diff ≤ 250 LOC (split otherwise)
- [ ] Conventional Commits header
- [ ] No secrets / PATs / API keys in diff (`git diff | grep -iE 'sk-|ghp_|key='`)
- [ ] If you changed a daemon: updated its `bin/<name>.README.md`
- [ ] If you changed Worker routes: updated `docs/cursor-service-api.md`
       and `docs/cursor-service.bruno.json`
- [ ] If you added a dependency: justified in PR body, lockfile updated
- [ ] CI green (`commitlint`, `cve-monitor` if deps changed)

## Testing locally

```bash
# Daemons (one-shot, no loop)
REPO_ROOT=$PWD python3 bin/axentx-bd-daemon.py --once

# Worker
cd cf-worker && wrangler dev
# → http://localhost:8787

# Lint Python
ruff check bin/

# Run CVE scan locally
osv-scanner --recursive .
```

## Templates

Bug report (open an issue):

```
**What broke**: <symptom>
**Where**: <daemon name / Worker route / file>
**Repro**: <minimal command or queue payload>
**Expected vs actual**:
**Logs**: <relevant log excerpt, redact secrets>
```

Feature request:

```
**Pain**: <real-world friction observed>
**Proposal**: <one-paragraph design>
**Affected stack**: <daemons / Worker / D1 / etc.>
**Roadmap fit**: <ROADMAP-100 # if any>
```

Runbook (for `docs/runbooks/<scenario>.md`):

```
## When this fires
## First-aid (≤ 60 sec)
## Diagnosis
## Fix
## Verify
## Postmortem template
```

## Escalation

Single-maintainer org (`@arkashira`). For anything with cost or security
implications, open an issue first; don't auto-bot it.

## Code style

- **Python**: 4-space indent, type hints on public funcs, `from __future__ import annotations`, no `print()` outside daemons (use `log()` from `axentx_pipeline`).
- **JS** (Worker): no transpiler, write to `compatibility_date` features, ES2023 OK.
- **Bash**: `set -euo pipefail`, quoted vars, `trap` for cleanup.
- **No comments that restate the code.** Comment why, not what.

## License

MIT. By contributing you agree your changes ship under the same license.
