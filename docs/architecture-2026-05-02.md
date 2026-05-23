# Architecture — surrogate-1 fleet (2026-05-02 redistribution)

> Replaces the implicit "everything on Mac via Hermes" topology that
> shipped pre-2026-05. Goal: zero-Mac dependency (except OCI security
> token), all agents always-on as daemons, queue-coordinated, free-tier
> friendly, multi-account.

## Topology

```
                ┌─────────────────────────────────┐
                │ Cloudflare Worker (free)        │
                │  /queue/* /mirror/* /seen/*     │   D1 + KV +
                │  /harvest/* /agent/heartbeat    │   Vectorize +
                │  + scheduled() canaries          │   Workers AI
                └───────┬───────────────────┬─────┘
                        │                   │
   ┌────────────────────┘                   └────────────────────┐
   │                                                              │
   ▼                                                              ▼
┌────────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐
│ GCP e2-micro       │   │ Kamatera 2B/8GB/50GB │   │ Codespace fleet × 7 acct │
│ 1 GB RAM, 24/7free │   │ $63/mo — 25d remaining│   │ basicLinux32gb, 8 GB     │
│                    │   │                      │   │ 60h/mo each = 420h/mo    │
│ COORDINATION       │   │ HEAVY DAEMONS        │   │ LLM PROXY + DEV          │
│ + light daemons    │   │ + scrape workers     │   │                          │
└────────────────────┘   └──────────────────────┘   └──────────────────────────┘
   │                              │                              │
   │     ┌────────────────────────┴────────────┐                 │
   │     │                                     │                 │
   ▼     ▼                                     ▼                 ▼
┌─────────────┐    ┌────────────────┐    ┌──────────────┐   ┌──────────────┐
│ HF Spaces   │    │ HF Datasets    │    │ HF Inference │   │ GH Actions   │
│ ZeroGPU     │    │ axentx/*       │    │ (Novita free,│   │ × 7 acct     │
│ training/   │    │ training-pairs │    │  Together,   │   │ 2000 min/mo  │
│ inference   │    │ harvested-pains│    │  Fireworks)  │   │ each =14k/mo │
└─────────────┘    └────────────────┘    └──────────────┘   └──────────────┘
```

## Where each daemon lives — and why

### GCP e2-micro (1 GB RAM, free, 24/7)

Coordination and fleet ops only. Anything memory-heavy goes to Kamatera.

| Daemon | Why GCP |
|---|---|
| `axentx-codespace-keepalive.service` | Pings codespace fleet during business hours; needs `gh` + GH_TOKEN, low RAM |
| `axentx-kamatera-terminator.timer` | Hourly oneshot via REST; Mac-independent kill switch |
| `surrogate-self-heal-daemon.service` | Restart-stuck-daemons watcher; tiny RAM |
| `surrogate-state-sync-daemon.service` | git pull-rebase-push of state branch; tiny |
| `axentx-canary-daemon.service` | E2E probe of pipeline + alert on 3-strike failure |
| `axentx-incident-responder-daemon.service` | Detect 5xx + GH Action failures, auto-rerun |
| `axentx-support-inbox-daemon.service` | Discord webhook listener; near-zero RAM |
| `axentx-scheduled-runner-daemon.service` | Always-on runner that fires inner scheduled jobs |
| `axentx-secret-watchdog.timer` | Token rotation health-check |
| `surrogate-discord-bot.service` | hermes-discord-bot (interactive with users) |
| `surrogate-watchdog.service` | OCI capacity poller (parallel to Mac watcher) |

### Kamatera 2B/8GB ($63 promo, ends day 28)

Heavy data work. Bulk pull, HTTP scraping, multi-thread streaming.

| Daemon | Why Kam |
|---|---|
| `axentx-research-daemon@{1,2,3}.service` | 3 parallel pain-mining workers; RAM-hungry |
| `axentx-pain-validator-daemon.service` | LLM-driven cross-source validation |
| `axentx-dataset-mirror-daemon.service` | 6-thread streaming mirror, 25 sources |
| `axentx-hf-flusher-daemon.service` | Continuous D1→HF drain |
| `axentx-bd / business / prd / design-thinking / marketing / architect / ux / qa / reviewer / security / perf / docs / pm / commit / release / content / trends / skill-synthesizer / customer-poll` | Pipeline stages, all queue-driven |
| `axentx-v1-warmup-daemon.service` | Periodic ping to surrogate-1 v1 Space |

### Codespace fleet (7 accounts × 60h/mo = 420h wall-clock, ~14h/day rotation)

LLM proxy + (next turn) dev-agent workers.

| Account | Codespace name | Role |
|---|---|---|
| ashirapit ⭐ | `ollama-llm-proxy-r49955gvjxqv3ww4` | Primary LLM proxy (qwen2.5-coder-7b Q4) |
| midnightgts | `ollama-llm-proxy-97vrxrxwjg45h7vwg` | LLM proxy #2 |
| luckyburster-lab | `ollama-llm-proxy-v6xpwqjvx4pq2x6qp` | LLM proxy #3 |
| surrogate-1 | (future) | LLM proxy #4 |
| axentx-tech | (future) | LLM proxy #5 |
| arkship-ai | (future) | LLM proxy #6 |
| ifusefreedomza | (future) | LLM proxy #7 |
| ashirap | ⛔ FORBIDDEN | Reserved for AI-free APIs + git clone 5000/h |
| midnightcrisis | ⛔ EXHAUSTED | 60h cap hit this month |

The `axentx_pipeline._call_codespace_ollama()` function rotates over the
list in `CODESPACE_LLM_URLS` (comma-separated) with per-URL cooldowns:
sleeping codespace returns 502 → cool that URL for 30s and try the next.

### GitHub Actions × 7 (next turn deploy)

2000 min/mo per account × 7 = **14,000 min/mo of free CI compute**. Each
account forks the harvest repo and runs the same workflow on its own
schedule, sharded by account hash so they don't dup-pull.

Targets per account:
- `mirror-shard-{account}.yml` — pull a specific dataset shard every 6h
- `train-shard-{account}.yml` — small LoRA training nightly via Colab
- `bench-shard-{account}.yml` — eval harness on test pairs

### HF Spaces (free, ZeroGPU 25k min/mo PRO)

Inference + training:
- `axentx/surrogate-1` — v1 inference Space (currently broken, deprecated)
- `axentx/surrogate-1-v2` (future) — v2 inference Space serving the LoRA
  trained from the streaming corpus

### Data plane

- **D1** (Cloudflare): `pipeline_items`, `seen_stamps`, `harvested_pains`,
  `agent_status`, `mirror_cursors`, `audit_log`, `space_health`, `kv_hits`
- **KV** (Cloudflare): hot-path mirrors (datasets, agent state) with 30s TTL
- **Vectorize** (Cloudflare): RAG embeddings (next turn)
- **HF Datasets** (axentx/*): `surrogate-1-pairs-{A,B,C,D}` (training corpus
  shards, ~1M pairs across all), `surrogate-1-harvested-pains` (raw pains),
  `surrogate-1-training-pairs` (curated, ready for SFT)
- **state branch** (git): orphan branch on `arkashira/surrogate-1-harvest`
  with daemon cursors, swarm-shared queues, lessons. Multi-writer with
  pull-rebase-push.

## Inter-VM communication

```
agent A on VM1
   │
   │  (1) push task to next stage
   ▼
POST /queue/push  →  D1.pipeline_items (stage='research', ...)
                              │
                              │  (2) any worker on any VM claims it
                              ▼
agent B on VM2  ←──  POST /queue/claim {stage='research'}  →  returns oldest
   │
   │  (3) work on it (uses LLM via _call_codespace_ollama which
   │      hits codespace fleet)
   ▼
POST /queue/advance {next_stage='bd', payload}
                              │
                              ▼
                  D1.pipeline_items (stage='bd', updated)
```

Shared dedup so no two VMs harvest the same URL twice:
```
research-daemon @ VM1: hash(url) → POST /seen/check  →  D1.seen_stamps
                                                  ↓ unseen
                                                fetch + parse
                                                  ↓
                                         POST /seen/mark
```

Cross-VM cursor for streaming pulls:
```
mirror-worker @ VM1: POST /mirror/lease  →  atomically claims (source, offset)
                          fetch HF rows
                     POST /mirror/advance
                          (lease released)
```

## Removed since 2026-05-02

- ❌ Mac LaunchAgents `codespace-keepalive` + `kamatera-auto-terminate`
  → moved to GCP systemd
- ❌ `KAMATERA_LLM_URL` and `_call_kamatera_ollama()` provider
  → LLM should NEVER run on Kamatera (8 GB RAM is for daemons + scrapers,
  not 5 GB ollama models)
- ❌ Hourly cron between dataset-mirror cycles → now continuous threaded streams
- ❌ 15-min HF flusher gap → now continuous adaptive batch
- ❌ `gh codespace ssh` debug failures → sshd feature added to devcontainer

## Failure-mode map

| Failure | Detection | Recovery |
|---|---|---|
| Codespace #N asleep | _call_codespace_ollama gets 502 | per-URL cooldown 30s, try next |
| All codespaces sleeping | All cooldowns hot | fall through to HF Inference (Novita free) |
| HF rate-limited | hf-flusher catches HfHubHTTPError 429 | adaptive backoff + smaller batch |
| Kamatera daily-budget alert | kamatera-terminator.timer | terminate at day 28, $0 charge |
| GCP unreachable | systemd Restart=always | self-respawn within 30s |
| Mac sleeps | nothing on Mac except OCI watcher | OCI watcher recovers on wake |
| Worker D1 outage | catch in cf_lease/advance | local cursor file fallback |
| All LLM providers 429 | Codespace-fleet exhausted last in chain | dev-daemon retries next cycle |
