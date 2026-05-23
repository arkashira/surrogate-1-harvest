# Surrogate-1 fleet vs. Hermes-on-Mac era — what's actually different

> Hermes was the bash-orchestrated dev-bot that lived in `~/.claude/bin`
> and ran on a single Mac. The current architecture (post-2026-05-02
> redistribution) is a 4-tier free-tier fleet. This doc is the honest
> comparison — including what was actually BETTER about Hermes.

## tl;dr

- **Compute available**: ~5× — single Mac (≈12 GB usable) → 1 GB GCP +
  8 GB Kam + 7 × 8 GB codespaces (336 GB-hours/day at full spin) +
  HF ZeroGPU 25k min/mo + 14k GH-Actions min/mo.
- **Always-on coverage**: Mac slept ~12h/day → fleet runs 24/7 with
  business-hours focus on codespace fleet (quota math).
- **Throughput receipts (2026-05-02)**:
  - Hermes era: ~6 pairs/h harvested (often zero — laptop closed).
  - Now (streaming, 2 VMs, threaded): ~94,000 pairs/h sustained, peak
    minute hit ~7,000 pairs/min.
  - = **~15,000× speedup** in harvest, plus full visibility via
    `/mirror/stats` + Discord alerts.
- **LLM provider chain**: 13-layer ladder with per-provider cooldowns
  vs. Hermes's single-shot Anthropic + occasional Groq. Ladder degrades
  gracefully through 429s instead of crashing the bot.
- **Multi-account**: 14 known GH accounts, 5 HF tokens (2 PRO),
  2 Lightning, 1 Kaggle, 2 Render, etc. — all rotated. Hermes had a
  single `gh auth` and one HF token.

## Where the new fleet wins

### 1. Continuous data harvest (the biggest single win)

**Hermes**: single-process, single-Mac, ran when laptop was open. Harvest
loop polled every 30 min. Result: roughly 50 raw pains/day on a good day,
zero on travel days.

**Now**: 33 daemons across 2 VMs + 7 codespaces. Streaming dataset-mirror
with cross-VM lease coordination via D1. Receipts:
- 12:00–12:22 UTC on 2026-05-02 = +34,498 training pairs in 22 minutes
  on file disk; 106,310 rows tracked across 25 sources in D1.
- HF flusher continuously drains D1 staging buffer to
  `axentx/surrogate-1-harvested-pains`. Adaptive batch 100→1000 on
  success, 1000→100 on rate-limit.

### 2. Free-tier rate-limit immunity

**Hermes**: when Anthropic hit 429, the bot died until next cycle.

**Now**: 13-layer ladder (`call_llm()` in `axentx_pipeline.py`):
1. Cerebras (CSK) — 8B fast
2. Groq — Llama-70B
3. SambaNova — Llama-405B
4. NVIDIA NIM — Llama-405B
5. OpenRouter (paid + free)
6. GitHub Models — `gpt-4o-mini` free 5/min
7. Mistral La Plateforme
8. CF Workers AI — `@cf/meta/llama-3.3-70b-instruct`
9. HF Router — `inclusionAI/Ling-2.6-1T` on Novita (free)
10. Codespace fleet — qwen2.5-coder-7b across 7 codespaces, per-URL cooldown
11. Gemini AI Studio — multiple keys
12. Workers AI Beta backup
13. v1 fallback model on HF Space (deprecated, zero-quota)

Per-provider cooldown registry: 429 → 120s, 402 → 24h, 5xx → 60s.
Pipeline never hangs on a single rate-limited provider.

### 3. Multi-VM coordination via Cloudflare Worker

**Hermes**: state lived in `~/.surrogate/state/` on the Mac. Lost when
laptop closed.

**Now**: D1 + KV + Vectorize behind the cf-worker. Routes:
- `/queue/push|claim|advance|stats` — pipeline stages
- `/seen/check|mark` — cross-VM dedup
- `/harvest/post|stats` — pain staging
- `/mirror/lease|advance|stats` — dataset cursor coordination
- `/agent/heartbeat` — fleet liveness ([dash/agents](https://surrogate-1-cursor.ashira.workers.dev/dash/agents))
- Schema is auto-bootstrapped on first call. No migrations required.

### 4. Customer-facing surface that survives sleep

**Hermes**: Discord bot ran on Mac → silent when laptop slept. Users
hit "no response" frequently.

**Now**: `surrogate-discord-bot.service` runs on GCP. Per-user memory
(file-backed JSON), persona-aware. Two-way: writes back poll results
into the original BUILD item.

### 5. Auto-cleanup + budget guardrails

**Hermes**: Mac was the budget — manual.

**Now**:
- `axentx-kamatera-terminator.timer` fires hourly on GCP, kills the
  Kam VM at day 28 of the 1MONTH300 promo (day 30 = billing).
- `axentx-secret-watchdog.timer` reminds before tokens expire.
- `incident-responder` auto-reruns failed GH Actions, redeploys Render,
  restarts dead daemons.

### 6. Knowledge that scales beyond the developer's head

**Hermes**: knowledge lived in `CLAUDE.md` and rules/. Single context.

**Now**: same files PLUS `~/Documents/Obsidian Vault/AI-Hub/knowledge/*`,
patterns/, sessions/, plus per-project knowledge index in
`/state/swarm-shared/done/*.json`. The full corpus is going on HF
(`axentx/surrogate-1-pairs-{A,B,C,D}` ≈ 1M pairs across coding/dialog/
commits/reasoning/iac).

## Where Hermes was actually better (the honest part)

### 1. Latency

Single Mac, single process, single in-memory state. Decisions in <1s.
The fleet adds D1 RTT (≈30-100ms per coordination call) plus codespace
wake-from-sleep (30-60s first call after idle). For one-shot tasks the
Mac was faster.

### 2. Debuggability

Hermes was bash + a few Python files. `tail -f ~/.surrogate/logs/*` told
you exactly what was happening. The fleet has 33 daemons spread across
2 VMs + 3 codespaces — debugging requires `journalctl -u <unit>` on the
right host, plus correlation across `/queue/stats`, `/mirror/stats`,
`/dash/agents`. Without the dashboard it's a maze.

### 3. Cost predictability

Mac was free (already owned). Fleet is "mostly free" but has soft
edges: Kam $63/mo for 25 days, HF PRO $9 × 2 = $18/mo, Modal credits
exhausted, Replicate is metered. We have to actively watch the
free-tier exhaustion.

### 4. State portability

Hermes state was a directory. tar + scp. Done.
Fleet state is split across D1 (CF), git state branch (3 remotes), HF
datasets, KV cache, Vectorize. Reconstituting requires the full
coordination plane to be alive.

### 5. Trust boundary

Hermes ran as the user, in the user's home dir. No cross-account
secrets juggling. The fleet has 14 GH PATs, 5 HF tokens, 2 Cloudflare
keys, etc. — `~/.note` is now 286 lines and grows.

## What's changed in the past hour (2026-05-02 round 2)

- Streaming dataset-mirror replaced 1h cron — 25 sources, 4+6 worker
  threads, cross-VM D1 lease coordination
- HF flusher continuous + adaptive batch (no more 15min cron sleep)
- All Mac LaunchAgents migrated to GCP systemd (only OCI watcher stays
  on Mac; OCI security_token requires browser auth)
- Customer-poll Discord spam fixed via persistent gate file
- Multi-codespace LLM rotation (3 endpoints active, 4 more scaffolded)
- Kamatera scrubbed of ollama (it's daemon-RAM territory, not LLM)

## What's still on the to-do list (next turn)

1. **Multi-project dev fleet** — Costinel/Vanguard/AxiomOps/Arkship/
   Workio currently have stale local-only commits. Need: set remotes
   on AXENTX/* repos (they exist but ashirapit can't see them — need
   to be added as collaborator, OR fork to ashirapit/*), then rotate
   dev-daemon across 7 codespaces × 5 projects = 35 parallel commit
   streams.
2. **GH Actions per-account** — 14k min/mo free, currently unused
3. **Kamatera SSH recovery** — user needs to:
   - Open `console.kamatera.com` → server `surrogate-watchdog-kam`
     → Web Console → run `ufw allow 22 && ufw reload`
   - Add IP whitelist for API in Kam dashboard (any IP, or current Mac)
4. **Long-poll daemons → streaming** — `research` (10 min poll),
   `content` (4h), `trends` (6h) should become continuous queue
   workers instead of cron-style sleepers.
5. **HF Space rebuild** — v1 inference Space is deprecated; v2
   should serve the LoRA from the new streaming corpus.

## Summary

The fleet is undeniably **bigger** — more compute, more accounts, more
redundancy, more throughput. Where Hermes solved one problem (one
developer, one laptop, no orchestration), the fleet solves the
"24/7 multi-tenant data harvesting + dev pipeline" problem at zero
marginal cost (within free-tier caps).

The price is operational complexity. Without dashboards + alerts +
the 8-page runbooks (`docs/runbooks/*`), the fleet is opaque and
fragile to mid-coordinate failures (D1 outage = pipeline frozen). The
next round of work is on observability + the multi-project dev fleet.

What the fleet is NOT yet better at:
- Single-user interactive feel (Hermes responded faster)
- "Why did X just happen" forensics (no unified trace UI yet)
- Survives total CF outage (worker is the single point of coordination)
