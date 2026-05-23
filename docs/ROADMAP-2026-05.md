# Roadmap — capability gaps + improvements (2026-05-02)

40 concrete features ranked by impact. **Must-have** = ships within 1 month or
blocks real production use. **Nice-to-have** = quality of life / polish.

Each row has a one-line "why" + estimated complexity (S/M/L). Implementation
notes deferred to per-feature ADRs when picked up.

---

## ⭐ MUST-HAVE (20)

| # | Feature | Why | Stack | Cmplx |
|---|---|---|---|---|
| 1 | Cursor "exhausted" + total tracking | Currently re-pulls finished datasets forever — cost + duplicate pairs | CF Worker + D1 | S |
| 2 | Worker auth (shared secret header) | Cursor service is internet-public; trivially abusable | CF Worker | S |
| 3 | hermes-worker RSS soft cap + graceful exit | 30s timeout misses slow-leak scripts that OOM the daemon | GCP daemons | S |
| 4 | Reviewer dynamic threshold (per-project bar) | Single 3-attempt cap is too coarse; discovery vs ops should differ | agent pipeline | M |
| 5 | DPO/SFT auto-pull into next training run | trainer.py doesn't fetch the self-improve dataset shards yet | trainer + HF | M |
| 6 | Eval suite scheduled (BFCL + agent leaderboard) | No automated regression on adapter quality between V19/V20 runs | bench scripts | M |
| 7 | D1 + Postgres backup to R2 (daily snapshot) | Single point of failure on cursor state + work-queue history | CF + cron | M |
| 8 | Secret rotation tracker | HF_TOKEN, GITHUB_TOKEN, KAGGLE_API_TOKEN not rotated since project start | observability | S |
| 9 | Audit log (every Worker hit + agent action) | Security req + debugging mystery state changes | CF Worker + D1 | S |
| 10 | Dataset license enforcement | License field unused; trainer might consume non-commercial data | trainer | S |
| 11 | Multi-LLM consensus reviewer (3-of-5 vote) | Single LLM verdict is noisy; consensus reduces false REJECT/APPROVE | reviewer daemon | M |
| 12 | Code-diff-aware reviewer (not just text) | Reviewer reads proposal *text* not the actual changed files | reviewer + git | M |
| 13 | GitHub commit signing (GPG or SSH) | Auto-bot commits unsigned → can't enforce branch protection w/ require-signed | commit daemon | S |
| 14 | Cost dashboard (per-component spend) | No visibility into Supabase/CF/HF/GCP costs; bills could grow silently | observability | M |
| 15 | Health pinger external (uptimerobot/pingdom) | Self-watchdog can't catch GCP-wide outage | monitoring | S |
| 16 | Knowledge index integration in agent prompts | knowledge_index.md exists but not fed to LLM as context | agent pipeline | S |
| 17 | Dedup across HF dataset sources | Same pair from different sources = wasted training step | dedup-bootstrap | M |
| 18 | Training data versioning (snapshots) | training-pairs.jsonl is append-only; no rollback to "v1.5 corpus" | training | M |
| 19 | OCI return playbook (when ticket clears) | Need scripted re-onboarding so we don't repeat 4-hr setup | ops docs | S |
| 20 | Discord bot Thai-aware system prompt | Current generic prompt; user is Thai, work context is Thai | discord-bot | S |

---

## 🌟 NICE-TO-HAVE (20)

| # | Feature | Why | Stack | Cmplx |
|---|---|---|---|---|
| 21 | CF Worker /metrics (Prom-style) | Visibility into per-endpoint latency + error rate | CF Worker | S |
| 22 | Per-dataset harvest dashboard | One glance: which datasets are stuck/exhausted/healthy | static page on R2 | M |
| 23 | Discord slash commands (/sg help, /sg status) | DMs/mentions only today; slash commands are discoverable | discord-bot | S |
| 24 | Per-agent bot identity (dev/qa/reviewer commit as different) | Easier to grep history; sane co-authors | commit daemon | S |
| 25 | Auto-docs from decisions/ → README contributions | 100+ decision records sit unread; convert to project docs | new daemon | M |
| 26 | Session replay debugger for agent runs | Re-run failed cycles deterministically with seed | qa daemon | L |
| 27 | A/B testing for prompts (track approval rates) | Optimize dev_system / reviewer_system prompts empirically | reviewer + metrics | M |
| 28 | Cron job dashboard (visual schedule + last-run) | hermes-jobs.json is text; humans want a calendar view | static page | M |
| 29 | Hot-reload daemon code (no systemctl restart) | Faster iteration cycle | daemon framework | M |
| 30 | Inter-agent NL chat log | "Reviewer to dev: please add tests" instead of just JSON history | agent pipeline | M |
| 31 | Project-specific personas | axiomops dev voice ≠ Costinel dev voice | dev daemon | S |
| 32 | Auto-bench on every Kaggle V19 epoch | catch regression mid-train, not after 12h | trainer | M |
| 33 | TTL on cursor entries (auto-expire) | Inactive cursors hold D1 rows forever | CF Worker | S |
| 34 | CF Worker canary deploys (% rollout) | Test new Worker version on 5% before full | wrangler | S |
| 35 | HF Hub release tags (semver on adapter) | "axentx/surrogate-1-9B-v1.5.3" easier to pin than commit SHA | trainer | S |
| 36 | Knowledge graph visualizer (Obsidian export) | Click-through structure of patterns/lessons/skills | static page | M |
| 37 | Time-of-day-aware scheduling | Run heavy jobs (mirror, eval) at off-hours UTC | hermes-scheduler | S |
| 38 | Progress tweet automation (milestones) | Auto-post "v1.5 hit 70% AIME" to X | new daemon | S |
| 39 | Pre-commit hook in agent commits | Lint/format before push instead of after | commit daemon | S |
| 40 | Fine-grained access logs in Postgres queue | Currently `claimed_by` is just int; want fully attributable | Supabase schema | S |

---

## Implementation order (highest impact first — what we'll do now)

Picking features that:
- Unblock other work (force-multipliers)
- Have minimal moving parts (low risk)
- Already have infra ready (CF Worker, D1, Postgres just deployed)

**Round 1 — ship today:**
- #1 cursor exhausted/total tracking  (D1 schema + Worker logic, deploys atomic with audit)
- #2 Worker auth (shared secret header)
- #9 audit log

**Round 2 — within the week:**
- #3 worker RSS cap
- #16 knowledge index in prompts
- #20 Discord Thai prompt

**Round 3 — within the month:**
- #4 reviewer dynamic threshold
- #5 DPO/SFT auto-pull
- #6 eval suite scheduled
- #11 consensus reviewer

The full backlog stays in this file — pick what's relevant when capacity opens up. Track per-feature work in dedicated ADR files under `docs/adr/`.
