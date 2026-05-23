# Surrogate-1 — honest scorecard (2026-05-02)

User asked: "เทียบให้ชื่นใจ แต่เอา fact จริง ๆ แบบไม่อวย ไม่เอาใจ"
("compare it nicely but with real facts, no flattery").

This is the honest delta — better and worse — vs the two reference points:
1. Vanilla Claude (Sonnet 4.5 / Opus 4)
2. The previous Hermes-on-Mac single-process setup

## 1. vs Vanilla Claude (Sonnet/Opus)

### Where Surrogate-1 is WORSE

| Dimension | Why worse | Severity |
|---|---|---|
| Code quality per commit | LLM chain caps at Llama-3.3-70B free tier. Many cycles produce filler "improve frontend" / "add docs" boilerplate that Claude wouldn't write | **Major** |
| Reasoning depth | max_tokens 1500-2500 per call vs Claude 200k. No real chain-of-thought. Decisions are shallow | **Major** |
| Multi-file refactors | Each cycle ≈ 1 file. Can't hold a 50-file refactor in context | **Major** |
| Tool use | None. Claude has function calling; we have file-based handoffs | **Medium** |
| Code review depth | Reviewer daemon uses same chain → catches obvious blockers, misses subtle bugs | **Medium** |
| Recovering from a bad path | Claude can backtrack a long conversation; we're single-shot per stage | **Medium** |

### Where Surrogate-1 is BETTER

| Dimension | Why better |
|---|---|
| 24/7 autonomy | 72 daemons, no human prompts needed |
| Cost | $0/mo vs $3-15/M tokens for Claude |
| Concurrency | 6 dev workers + 3 research + ... in parallel, indefinitely |
| Persistence | state branch on git survives every VM restart; chat-memory survives every bot restart |
| Continuous discovery | research → validator → bd → design → business → marketing → prd → dev pipeline is unique |
| Repeatable pattern matching | RAG over our own decisions makes "remember the past" deterministic |
| Specialization | 30+ role-tuned prompts (architect/security/ux/perf/...) vs Claude's general assistant |

**Net**: For a single high-stakes feature, Claude wins on quality. For continuous lower-quality output at scale, Surrogate wins. They serve different jobs.

---

## 2. vs Hermes-on-Mac (the old single-process setup)

### Where today is BETTER

| Dimension | Old Hermes | Today | Delta |
|---|---|---|---|
| Visibility | terminal log | /dash/agents 72 reporting + state branch | Major |
| Resilience | mac sleep = pipeline dies | 2 VMs + state-sync every 5min + LaunchAgent watchers | Major |
| Throughput | 1 process | 30 GCP + 42 Kamatera = 72 daemons | ~70x |
| Recovery | manual restart | systemd Restart=always + self-heal-daemon + incident-responder | Major |
| Discoverable history | logs only | training-pairs.jsonl + state branch + skill library | Major |

### Where today is WORSE

| Dimension | Old Hermes | Today | Delta |
|---|---|---|---|
| Failure modes | mac crash | LLM 429s, queue starvation, cursor saturation, KV quota, SSO grant, capacity blocks, dpkg locks, account quota, SSH key gaps, ... | **Many** |
| Quality of any single output | you prompted Claude/Sonnet directly | LLM chain hits free-tier limits → degraded mid-tier model often picks up | Worse |
| End-to-end latency | inline | filesystem queues + RPC hops + state-sync delay | Worse |
| Setup complexity | 1 systemd service | 30+ daemons, 2 VMs, multiple secrets stores, CF Worker, D1, KV, queue hierarchies | Worse |
| Debug burden | grep 1 logfile | journalctl -u N services across 2 hosts + dashboard + state branch | Worse |

**Net**: We did more, broke more, debugged 4 distinct bugs in the last single regression sweep. Operational cost (your attention) higher than before, output throughput dramatically higher.

---

## 3. The DISTRIBUTED setup specifically (after Kamatera joined)

### Confirmed problems user noticed

| Problem | Root cause | Status |
|---|---|---|
| 2 VMs run same research workers independently | No shared dedup. Each fetches the same Reddit/HN/SE URLs | **FIXED** 2026-05-02: D1 seen_stamps + worker /seen/{check,mark} routes |
| Pipeline queues per-VM (no work sharing) | Filesystem queues local to each host | **Pending**: D1-backed pipeline queue migration |
| HF rate-limited under heavy load | 272k 429s/7d earlier | **FIXED** 2026-05-02: D1 harvested_pains staging buffer + axentx-hf-flusher-daemon |
| Workio commit drought | LLM 429s land on workio cycle in particular | **Pending**: dedicated worker pin / rotation rebalance |
| Auto-release lag | 4h cadence, ≥5 commits | **FIXED** 2026-05-02: 1h cadence, ≥3 commits |
| Heartbeat collision (dashboard showed ~30, not 60+) | D1 PK on agent only | **FIXED** 2026-05-02: agent name = `<host>/<role>` |

### What's still actually fragile

1. **LLM chain rate-limits cascade** — when Groq hits TPD, Cerebras + Kimi soak up overflow but quality drops. We have no real budget headroom.
2. **State-branch sync** is best-effort 5min. A VM that crashes between syncs loses 0-5min of work. Not catastrophic but real.
3. **Free-tier headroom is tight everywhere** — KV writes (1k/day blew once), CF Worker 100k/day, Groq 100k tokens/day, OCI A1 capacity. One of these will be the next outage.
4. **Code generated is mostly filler** — most "discovery cycle 20260502-..." commits are not user-visible features. They're agent self-priming.

---

## 4. Net answer to "is this better or worse?"

**Better than Hermes-on-Mac**: yes, decisively, on every dimension except complexity.

**Better than vanilla Claude for general work**: no. For specific niche of "always-on lower-quality discovery + low-quality continuous code production": yes.

**Cost**: $0 + $63/30d Kamatera promo (auto-terminating). Claude API on equivalent volume would be $400-2000/mo.

**The honest pitch**: this is a *discovery + light-engineering* fleet, not a Claude replacement. Treat the commits as drafts to triage, not production-ready PRs. The real value is the *pipeline structure* (research → validator → bd → ...) and the *training corpus* it accumulates for v2 model fine-tuning. The current LLM chain is a placeholder; v2 trained on the harvested decisions should close the quality gap.

---

Updated 2026-05-02 after deep regression sweep + Kamatera VM join.
