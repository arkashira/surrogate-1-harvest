# Surrogate-1 Agent / Daemon / Cron Audit — 2026-04-29

Audit timestamp: 2026-04-29 16:25 ICT
Auditor: ops agent
Method: live HF Space probe + GitHub API + Mac process inspection

---

## HF Space (axentx/surrogate-1)

| Field | Value |
|---|---|
| Stage | RUNNING (status server :7860 returned 200) |
| Hardware | cpu-basic 16 GB (per start.sh comments) |
| Public root URL | **HTTP 500** ("Sorry, there is an error on our side") — `huggingface.co` proxy serves an error template |
| Status API (`/api/status`) | **HTTP 200**, container alive |
| Logs API (`/logs`) | HTTP 200 |
| HF API (`get_space_runtime`) | hit 429 rate limit on first call — could not retry |

Container internals (from `/api/status` JSON, ts=2026-04-29 09:20:28 UTC):
- `daemons_running: 14` (start.sh launches 17 → 3 missing)
- `training_pairs: 46,481`
- `dedup_hashes: 322,457`
- `agentic_urls_visited: 58,425`
- `ledger_repos: 283`
- `skills_synthesized: 0` (skill-synthesis daemon yields 0 — likely stuck)
- `models_loaded: granite-code:8b, qwen2.5-coder:14b, nomic-embed-text, devstral:24b, yi-coder:9b, gemma4:e4b`

Critical log signals (from `/logs`):
- `tee: /home/hermes/.surrogate/logs/training-push.log: Input/output error` — repeated. **/data mount is broken or read-only on parts of fs**
- `Cannot connect to host discord.com:443` — confirms egress block (start.sh expects this, switches to webhook)
- Cron is running, last training-push fired 09:26:10 UTC and pushed 9 lines

### Daemons (from start.sh — 17 expected, 14 reported running)

| # | Name | Purpose | Expected | Actual | Fix |
|---|------|---------|----------|--------|-----|
| 1 | redis-server | priority queue + dedup cache | running | running | — |
| 2 | ollama serve | LLM backend (CPU mode) | running | running | — |
| 3 | hermes-discord-bot.py | Discord gateway bot | gateway test → skip if blocked | started but errors | egress to discord.com blocked; webhook fallback works. Cosmetic noise — discord.com is correctly skipped at boot, but bot somehow still launched (logged `logged in as Surrogate#9979` then crashes). Likely racing the test. **Fix**: tighten the connectivity gate — require 3 successful pings before launch, OR remove bot entirely (use webhook only). |
| 4 | scrape-daemon.sh (parallel=8) | continuous domain scrape | running | unclear | check `scrape-continuous.log` next boot for activity |
| 5 | agentic-crawler.sh (parallel=6) | BFS link discovery | running | running (logs show URL frontier) | — |
| 6 | github-agentic-crawler.sh | 4 PAT × 5K/h GitHub crawl | running | unknown | needs log inspection |
| 7 | hf-dataset-discoverer.sh | continuous HF dataset hunt | running | running (boot.log) | — |
| 8 | auto-orchestrate-continuous.sh (4 workers) | dev-loop runners | running | running (boot.log) | — |
| 9 | self-heal-watchdog.sh | RAM <85% guard, restart stuck shards | running | running (boot.log) | — |
| 10 | gh-actions-ticker.sh | dispatch GH workflows every 60s | running | **running BUT counterproductive** | **CRITICAL — see Fix #1 below.** Tick fires `workflow_dispatch` every 60s × 2 repos. With each dispatch matrix=20 jobs and 8-min timeout, jobs queue depth is unbounded once free-tier 20-concurrent slot cap is exceeded. Currently 200/200 last runs are `queued` for 60+ minutes each, not running. Throttle from 60s to 600s OR stop entirely. |
| 11 | llm-burst-generator.py | 8-LLM synthetic pair gen | running | running | — |
| 12 | bulk-ingest-parallel.sh (6 shards) | parallel HF dataset ingest | running | running | — |
| 13 | parquet-direct-ingest.sh (2 DLs) | parquet shard direct download | running | running | — |
| 14 | skill-synthesis-daemon.sh | extract skills from cloned repos | running | running BUT 0 output | `skills_synthesized: 0` after ~24h uptime → daemon is alive but producing nothing. Inspect `skill-synthesis.log`. Likely missing input (no parsable repos cloned, or parser regex broken). |
| 15 | hermes-cron.sh | cron loop master | running | running (cron.log shows ticks) | — |
| 16 | hermes-status-server.py | FastAPI :7860 | running | running | — |
| 17 | dataset-mirror.sh (boot kickoff) | bulk-clone 30 community SFT mixes | one-shot | unknown | check stamp file `~/.surrogate/.dataset-mirror-done` next session |

**Boot kickoffs (one-shot, fire-and-forget):**
- `dedup-bootstrap.sh` (one-time on first boot)
- `dataset-enrich.sh` (boot trigger to skip cron M%60 wait)
- `kaggle-trainer.sh` (submit notebook to T4)
- `lightning-trainer.sh` (submit to H200)
- `dataset-mirror.sh` (mirror SFT mixes)

These are NOT in the 14-running count — they exit after their one job.

**Cron-loop daemons inside `/tmp/hermes-cron.sh`** (every M minutes):
- `surrogate-dev-loop.sh 1` — every 2 min
- `work-queue-producer.sh` — every 5 min
- `push-training-to-hf.sh` — every 3 min ✅ (last fire 09:26 UTC, 9 pairs pushed)
- `surrogate-research-apply.sh` — every 30 min (M%30=15)
- `scrape-keyword-tuner.sh` — every 60 min (M%60=0)
- `surrogate-research-loop.sh` — every 6 hr (M%360=30)
- `dataset-enrich.sh` — every 60 min (M%60=5)
- `surrogate-self-ingest.sh` — every 15 min (M%15=0)
- `rag-vector-builder.sh` — every 30 min (M%30=12)
- `synthetic-data-from-rework.sh` — every 30 min (M%30=7)
- `refresh-cve-feed.sh` — daily 04:00 UTC
- `scrape-sre-postmortems.sh` — daily 05:00 UTC
- `expand-role-keywords.py` — daily 06:00 UTC
- `kaggle-trainer.sh` — every 90 min (M%90=5)
- `lightning-trainer.sh` — every 6 hr (M%360=45)

These are PROCESS-FORK pattern (not long-lived), so do not show in `daemons_running` count.

---

## Mac CLI (Niflheim laptop)

### crontab -l
**EMPTY** — `crontab: no crontab for Ashira`. Good (Mac=CLI rule satisfied here).

### launchctl list (filtered for surrogate/axentx/hermes/claude/falkor/chromadb)
Only Claude desktop app entries:
- `com.anthropic.claudefordesktop.55433773.55433780`
- `com.anthropic.claudefordesktop.ShipIt`

**No surrogate/hermes/axentx launchd jobs.** Good.

### Heavy processes that VIOLATE Mac=CLI rule

| PID | Started | Cmd | Severity |
|-----|---------|-----|----------|
| 50173 | Fri 24-Apr 16:32 (5 days running) | `bash /Users/Ashira/.claude/bin/surrogate --auto` (script DELETED but bash holds FD 255r open; cwd=`~/axentx/Vanguard`; only ~1.6 KB RSS) | **LOW** — zombie-like, 0% CPU, only spawns `sleep 60` children. The actual file `~/.claude/bin/surrogate` is gone, but the running bash still has it open. Safe to `kill 50173` to clean up. |
| 37771 | Today 16:21 | `oci compute instance launch --shape VM.Standard.A1.Flex 4 OCPU 24GB` for `surrogate-ingest` instance | **MEDIUM-HIGH** — already EXITED by audit time. This was a cloud provisioning call from THIS Claude session, not a continuous daemon. NOT a violation, but worth noting an OCI A1 instance was launched in `ap-singapore-1` (free-tier ARM, $0/mo) earlier today. |
| 37133 | Today 16:13 | `sleep 720 && python3 ... kaggle.com/api/v1/kernels/status` (poll for `sg1-v1-t4x2-1777452753`) | **LOW** — scheduled one-shot poll to check Kaggle kernel state. Not a continuous daemon. Will exit after one poll. |

**Mac=CLI rule status: NEAR-COMPLIANT.** No real heavy daemons. The 5-day-old zombie bash (50173) should be killed for cleanliness.

---

## GitHub Actions

| Repo | Workflow | Schedule | Last 200 runs status | Last conclusion |
|------|----------|----------|----------------------|-----------------|
| arkashira/surrogate-1-runner | bulk-ingest (active) | `*/5 * * * *` + workflow_dispatch every 60s by ticker | **200/200 queued** | last completed=2026-04-28 15:30 UTC → `failure` (mostly `cancelled` shards, only shard 12 succeeded) |
| ashiradevops-alt/surrogate-1-runner | bulk-ingest (active) | identical | **200/200 queued** | identical |
| arkashira/moltbot | (older project) | n/a | n/a | last push 2026-01-28 |
| arkashira/codereviewer | (older project) | n/a | n/a | last push 2025-06-05 |

**Critical finding**: GitHub Actions queue is FULLY SATURATED. Both repos have backed up since 2026-04-28 ~15:30 UTC (~17.5 hours of queued runs at audit time). Free-tier accounts get 20 concurrent slots; ticker dispatches a new 20-job matrix every 60s ⇒ queue grows by 20 × 60 = 1200 jobs/hr. Eventually GitHub auto-cancels older queued runs (no completion since ~24h ago).

Combined with hf-space-ingest's 4-min timeout and 6-shard `bulk-ingest-parallel`, the ingest pipeline depends primarily on the HF Space + Lightning + Kaggle, NOT GitHub Actions. The GH ingest path is currently **decorative noise**.

Repos with no surrogate-related workflows: `~/develope/surrogate-1-train` (just scripts, no `.github/`), `~/develope/hf-space-ingest` (push-target Space, no `.github/`), `~/develope/modal-ingest` (just `app.py`, no `.github/`). These are deploy-targets/sources, not runners.

---

## Local services (Mac)

| Service | Port | Status | Notes |
|---------|------|--------|-------|
| redis-server (PID 18565) | 6379 | LISTEN | local cache, lightweight |
| ollama (PID 93230) | 11434 | LISTEN | per CLAUDE.md "local LLM disabled" — but still bound. CPU/RAM idle. |
| litellm-proxy | 4100 | NOT LISTENING | per CLAUDE.md it's the failover route, not currently bound |
| chromadb | 8000 | NOT LISTENING | per CLAUDE.md "vector DB disabled" — confirmed |
| falkordb | 6379 (shared with redis?) | NOT LISTENING separately | per CLAUDE.md "graph DB still works" — but NO process bound. **Stale claim in CLAUDE.md.** |

---

## CLAUDE.md crons / scheduled tasks references

`~/.claude/memory/MEMORY.md` index:
- Only 3 project memory files referenced; no scheduled task definitions live in memory.
- `project_surrogate1_state.md` is referenced but **does not exist on disk** (Read failed). Possibly graduated already to `~/.claude/memory/graduated/`.

No active CronCreate / launchd entries for Claude memory automation.

---

## Summary

| Metric | Value |
|--------|-------|
| Total daemons audited | 17 in start.sh + 5 boot one-shots + 15 cron entries + 2 GH workflows + 4 local services = **43 components** |
| HF Space daemons running | 14 / 17 (3 unaccounted-for, possibly counted differently) |
| HF Space daemons broken/zero-output | 2 (skill-synthesis = 0 output, discord-bot = crashing in loop but cosmetic) |
| HF Space io errors | tee/mkdir to `/data/...` — **broken /data mount partially** |
| GitHub Actions runners | **0 / 200 last runs completed** — queue is saturated, ticker is the cause |
| Mac launchd / cron | 0 surrogate-related → clean |
| Mac stale processes | 1 zombie bash (PID 50173, 5 days, harmless) |
| Local services per CLAUDE.md | 2/4 actually listening (redis, ollama). chromadb + falkordb claims are stale. |

### Fix priorities (top 3)

1. **STOP / THROTTLE `gh-actions-ticker.sh`** — currently dispatching every 60s, both repos have 200/200 jobs queued for ~17h with zero completions. Either (a) raise `GH_TICK_SEC=600` (one dispatch per 10 min per repo = ~12 jobs/hr each, well under 20-concurrent cap) and let cron `*/5` handle the rest; or (b) disable ticker entirely since cron alone is sufficient at 5 min intervals × 20 shards = manageable. Without this fix the GH ingest path produces zero output.

2. **Fix HF Space `/data` mount write errors** — `tee: /home/hermes/.surrogate/logs/training-push.log: Input/output error` and `mkdir: write error: Input/output error` are repeated. The push-training-to-hf cron writes 9 pairs successfully though, so writes to /data/training-pairs.jsonl work but writes to /data/logs/* are failing. Likely a stale symlink or partial mount issue. Investigate `ls -la /data/logs/` on Space and recreate dir + perms. Currently logs are partly being eaten.

3. **Investigate `skill-synthesis-daemon` 0 output** — `skills_synthesized: 0` after multi-day uptime. Either the daemon has a parsing bug, no input (cloned repos missing under `/data/projects/`), or the regex finds nothing. Cheap fix: tail `~/.surrogate/logs/skill-synthesis.log` next deploy and grep for "synthesized" / "skipped" / "ERROR". This is the only feedback loop that turns scraped repos into something model-usable.

### Lower-priority cleanups

- Kill stale Mac PID 50173 (`bash surrogate --auto` from 5 days ago, 0 work).
- Update CLAUDE.md `falkordb still works` claim — not bound to any port.
- Remove discord-bot launch entirely (egress to discord.com blocked → crash loop, webhook fallback already works fine).
- Decide which path is the source of truth for ingest: HF Space (current actual workhorse) vs GH Actions (currently decorative). If GH stays, fix ticker; if not, archive `surrogate-1-runner` workflow.
