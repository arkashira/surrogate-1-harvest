# Architecture — surrogate-1 / axentx / hermes

> ROADMAP-100 #84. ASCII diagrams (C4 Container + Component) of the live
> autonomous-pipeline stack. Mirrors the implementation as of commit
> [`5f53d2c`](../README.md). Kept in repo so the mental model survives
> outages and onboarding.

## 1. System context

```
                         +--------------------+
                         |   Reddit / HN /    |
                         |   ProductHunt /    |
                         |   GitHub trending  |
                         +---------+----------+
                                   | (HTTP scrape, no API keys)
                                   v
+----------+     +----------+   +-----+    +------------+    +-----------+
| Discord  |<--->|  Hermes  |<->|  GCP|<-->| 11-LLM     |<-->| HF Hub    |
|  guild   |     |  HF Space|   | e2- |    | provider   |    | datasets/ |
| (humans) |     | (UI/poll)|   | mcrV|    | chain      |    | adapters  |
+----------+     +----------+   +--+--+    +------------+    +-----------+
                                   |                              ^
                                   v                              |
                         +-------------------+              +-----+-----+
                         |  CF Worker +      |<------------>| Supabase  |
                         |  D1 + KV +        |   (work      | (work     |
                         |  Queues + AI +    |    queue)    |  queue,   |
                         |  Vectorize +      |              |  pgvector)|
                         |  Pages + Cron     |              +-----------+
                         +-------------------+
```

Actors:

| External | Purpose |
|---|---|
| Reddit/HN/PH/GH-trending | Pain-point + opportunity discovery (read-only HTML scrape) |
| HF Hub | Stores adapters + datasets (9.56 TB) |
| 11-LLM ladder | Groq → Cerebras → OpenRouter → Gemini → Workers AI → … |
| Supabase | Cross-region work queue (FOR UPDATE SKIP LOCKED) |
| Discord guild | Human-in-the-loop poll + notifications (Thai-aware) |

## 2. Container view

The whole system is **two coordinated runtimes** plus a model store:

```
+-------------------------------------------------------------------+
|  GCP e2-micro VM (1 vCPU / 1 GB) — daemon host                    |
|                                                                   |
|  systemd ----- 22 daemon units ----- /opt/surrogate-1-harvest     |
|     |                                                             |
|     +-- axentx-research-daemon.py    (pain miner)                 |
|     +-- axentx-trends-daemon.py      (opp scanner)                |
|     +-- axentx-bd-daemon.py          (triage)                     |
|     +-- axentx-design-thinking-d.py  (validate)                   |
|     +-- axentx-business-daemon.py    (BMC)                        |
|     +-- axentx-marketing-daemon.py   (positioning)                |
|     +-- axentx-prd-daemon.py         (PRD → tasks)                |
|     +-- axentx-architect-daemon.py   (ADR for NEW-PRODUCT)        |
|     +-- axentx-dev-daemon.py × 6     (per-project rotation)       |
|     +-- axentx-reviewer-daemon.py    (code review)                |
|     +-- axentx-qa-daemon.py          (TDD test plan)              |
|     +-- axentx-security-daemon.py    (sec gate)                   |
|     +-- axentx-perf-daemon.py        (perf gate)                  |
|     +-- axentx-commit-daemon.py      (push to axentx repos)       |
|     +-- axentx-docs-daemon.py        (README/CHANGELOG)           |
|     +-- axentx-release-daemon.py     (semver tag, daily)          |
|     +-- axentx-content-daemon.py     (blog/social copy)           |
|     +-- axentx-pm-daemon.py          (sprint state machine)       |
|                                                                   |
|  Shared state: state/swarm-shared/{stage}-queue/*.json (file q)   |
+--------------------+----------------------------------------------+
                     |
                     | reads/writes via HTTPS
                     v
+-------------------------------------------------------------------+
|  Cloudflare edge — surrogate-1-cursor                             |
|                                                                   |
|  Worker (worker.js) ---- routes ----                              |
|     /health, /, /dash       (read-only)                           |
|     /cursor/<slug>          (GET cursor state)                    |
|     /cursor/<slug>/advance  (POST, auth)                          |
|     /datasets               (GET list / POST upsert)              |
|     /tasks/push             (enqueue → Queues)                    |
|     /ai/<model>             (proxy → Workers AI, 12th provider)   |
|     /audit, /metrics        (audit log + Prom metrics)            |
|                                                                   |
|  Bindings:                                                        |
|     D1   — surrogate-1-cursor      (cursors, datasets, audit)     |
|     KV   — CACHE                   (60s TTL on hot reads)         |
|     AI   — Workers AI              (12th LLM provider)            |
|     Q    — surrogate-1-tasks       (3rd queue backend)            |
|     Vec  — 1819-chunk knowledge    (RAG over harvest knowledge)   |
|     Cron — */5 *  (housekeeping)                                  |
+-------------------+-----------------------------------------------+
                    |
                    v
+-------------------------------------------------------------------+
|  HF Hub                                                           |
|     ashirato/* models, surrogate-1-* adapters, 9.56 TB datasets   |
|     6 HF Spaces: hermes-* (UI/inference fallbacks)                |
+-------------------------------------------------------------------+
```

Why the split? VM does heavy CPU + LLM-orchestration work; Worker handles
edge state (cursors, audit, datasets index) at zero cold-start cost and
serves the dashboard. HF Hub is the durable model + data store both runtimes
read from.

## 3. Pipeline flow (the 12-stage agent loop)

```
                     ┌─────────────────────────────┐
                     │  Pain + Opportunity sources │
                     │  reddit / HN / PH / GH      │
                     └─────────────┬───────────────┘
                                   │
       ┌───────────────────────────┴───────────────────────┐
       v                                                   v
 ┌─────────────┐                                  ┌────────────────┐
 │  research   │  ← pain points                   │     trends     │  ← weekly opp scan
 └──────┬──────┘                                  └───────┬────────┘
        │                                                 │
        └────────────────► research-queue ◄───────────────┘
                                   │
                                   v
                            ┌────────────┐   PASS  → done/
                            │     bd     │
                            └─────┬──────┘   EXTEND/NEW-PRODUCT
                                  v
                            ┌────────────┐
                            │  design-   │   PROCEED/REJECT
                            │  thinking  │
                            └─────┬──────┘
                                  v
                            ┌────────────┐
                            │  business  │   BUILD/NO-GO  (BMC + sizing)
                            └─────┬──────┘
                                  v
                            ┌────────────┐
                            │ marketing  │   (positioning + competitor map)
                            └─────┬──────┘
                                  v
                            ┌────────────┐
                            │    prd     │   ── if NEW-PRODUCT ──► architect ──┐
                            └─────┬──────┘                                     │
                                  │ ◄───────────────── ADR feedback ───────────┘
                                  │ epics + stories
                                  v
                            ┌──────────────┐  rotation: 6 projects × 6 focus =
                            │   dev × 6    │  Costinel/Vanguard/Airship/Workio/
                            │ (per-project)│  Axiomops/Surrogate × discovery/
                            └──────┬───────┘  design/backend/frontend/quality/ops
                                   v
                            ┌────────────┐
                            │  reviewer  │   APPROVE/REJECT (dynamic threshold)
                            └─────┬──────┘
                                  v
                            ┌────────────┐
                            │     qa     │   PASS/BLOCK
                            └─────┬──────┘
                                  v
                            ┌────────────┐    ┌────────────┐
                            │  security  │ +  │    perf    │   parallel gates
                            └─────┬──────┘    └─────┬──────┘
                                  └────── merge ────┘
                                          v
                                  ┌────────────┐
                                  │   commit   │   git push to axentx/<project>
                                  └─────┬──────┘
                                        v
                                  ┌────────────┐
                                  │    docs    │   README / CHANGELOG patch
                                  └─────┬──────┘
                                        v
                                  ┌────────────┐
                                  │  release   │   24h: semver tag + GH Release
                                  └─────┬──────┘
                                        v
                                  ┌────────────┐
                                  │  content   │   blog/social from shipped
                                  └────────────┘
```

Queue backends, in priority order:

1. **File queue** (`state/swarm-shared/<stage>-queue/*.json`) — local, atomic via `os.rename`.
2. **Supabase** (`work_queue` table, `FOR UPDATE SKIP LOCKED`) — cross-region.
3. **CF Queue** (`surrogate-1-tasks`) — burst absorber, 3rd-tier.

Each daemon polls its own queue (`POLL_SEC` env), claims an item, calls the
LLM ladder via `axentx_pipeline.call_llm()`, and `advance()`s the item to
the next stage. All transitions also write a row to `agent_decisions` (D1) for
SFT/DPO training-pair extraction.

## 4. Component view — single daemon

```
+--------------------------------------------------------------+
|  axentx-<role>-daemon.py                                     |
|                                                              |
|  daemon_loop(role, POLL_SEC) ────                            |
|     while True:                                              |
|        item = pick_oldest(<role>_queue)                      |
|        if not item: sleep(POLL_SEC); continue                |
|                                                              |
|        prompt = render(SYSTEM, item)         ← role-specific |
|        out    = call_llm(prompt, system)    ← 11-provider    |
|        verdict, payload = parse(out)                         |
|                                                              |
|        if verdict == PASS:  fail(item, reason)               |
|        elif verdict == OK:  advance(item, next_stage)        |
|        else:                advance(item, prev_stage)        |
|                                                              |
|        log_decision(item, role, verdict, prompt_hash)        |
+--------------------------------------------------------------+
```

Idempotency: each item has a UUID; `advance()` writes the next-stage file
with the same UUID, so re-runs deduplicate. Failures log to
`logs/axentx-<role>-daemon.log` and re-queue with backoff.

## 5. Deploy / topology

| Layer | What runs there | Provisioning |
|---|---|---|
| GCP e2-micro | 22 systemd-managed daemons + Hermes app server | `systemd/*.service` units, `start.sh` bootstrap |
| Cloudflare | Worker + D1 + KV + Queues + Vectorize + Pages | `cf-worker/wrangler.toml`, `cf-worker/schema.sql` |
| HF Hub | Adapters, datasets, 6 Spaces (hermes-*) | `huggingface-cli` push from VM |
| Kaggle | V19 trainer (LoRA on T4 / P100 / A100) | Notebook in `KAGGLE-V18-LAUNCH.md` |
| Supabase | Work queue + pgvector | Schema in `cf-worker/schema.sql` mirror |

## 6. Read next

- [README.md](../README.md) — runtime entrypoint
- [FEATURES.md](../FEATURES.md) — capability inventory
- [ROADMAP-100.md](./ROADMAP-100.md) — 100-feature plan
- [bin/](../bin/) — daemon source + per-daemon `*.README.md` (ROADMAP #89)
- [cf-worker/](../cf-worker/) — edge service source
