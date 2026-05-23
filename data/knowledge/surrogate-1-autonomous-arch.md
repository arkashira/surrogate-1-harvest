---
tags: [surrogate-1, architecture, autonomous, devsecops, sre, self-improvement]
created: 2026-05-01
status: live
applies-to: surrogate-1 v1.2-research and onward
---

# Surrogate-1 — Autonomous Pipeline Architecture (V8 / 2026-05)

> One-page mental model for the whole stack. Drill-down docs live in `trends-2026/*.md`.

## 0. Goal

A 7B Qwen2.5-Coder LoRA fine-tune that
- ships code 24×7 with no human in the loop,
- patches infra incidents within ≤5 min of detection,
- ships new features after auto-recon of competitors,
- gets measurably better every refresh cycle from its own logged outcomes,
- never makes a wrong destructive action (HardGuards-enforced).

## 1. Component map

```
                      ┌─────────────────────────────────────────────┐
                      │ Knowledge Base (Obsidian / .claude/memory)  │
                      │  • coding-llm-frontier.md                   │
                      │  • devsecops-sre-agentic.md                 │
                      │  • autonomous-24x7.md                       │
                      │  • self-improvement.md                      │
                      └────────────┬────────────────────────────────┘
                                   │ informs
                                   ▼
   ┌──────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
   │  Training pipeline   │    │  Inference / serve      │    │  Autonomous daemons    │
   │  bin/kaggle-trainer.sh    │  HF Spaces (PRO ZeroGPU)│    │  bin/v2/autonomous-*.sh│
   │  V8 = SFT stack       │    │  ashirato + surrogate1  │    │  + watchdog + improve │
   └──────────┬───────────┘    └────────────┬───────────┘    └────────────┬───────────┘
              │ produces                    │ serves                    │ observes / acts
              ▼                             ▼                            ▼
     axentx/surrogate-1-7B-v1.X      <space>.hf.space/api/predict   ~/.surrogate/state/
                                                                     outcomes.jsonl
                                                                     idempotency.jsonl
                                                                     queue/<ts>.json
                                              ▲                            │ feeds
                                              │ swap-zerogpu-lora.sh       ▼
                                              └─────────────────────  self-improve.sh
                                                                        │
                                                                        ▼
                                                                  axentx/surrogate-1-self-traces
                                                                  axentx/surrogate-1-pref-kto
                                                                  axentx/surrogate-1-skills
                                                                        │
                                                                        ▼
                                                                  next training round
```

## 2. The 4 daemons (24×7 background)

| Daemon | Role | Cadence | Calls Surrogate? | Can apply changes? |
|---|---|---|---|---|
| `autonomous-sre.sh` | probe HF Spaces / datasets / GH Actions / ZeroGPU; on anomaly → diagnose → verify → auto-heal | every 5 min | yes | yes (only after verifier passes) |
| `autonomous-release.sh` | recon HN/GH/PH; build spec; CISC-vote 3 patch candidates; open draft PR | every 1 hr | yes (×3 per cycle) | no (PR draft only) |
| `self-improve.sh` | aggregate outcomes; build SFT/KTO/skills datasets; trigger next training | daily/weekly | no | no |
| `watchdog.sh` | observe outcomes + daemon processes; kill on loop / cascade / rate-spike / audit-gap | every 1 min | no | no (kill-only) |

## 3. The shared safety gate

Every action that any autonomous daemon would apply runs through one funnel:

```
candidate change → idempotency.py check → verifier-ensemble.py
                                              │
                            ┌─────────────────┼──────────────────┐
                            │                 │                  │
                       9 layers:    HardGuards (14 refuse rules) │
                       1. ast            (in code, not prompted) │
                       2. lint                                   │
                       3. typecheck                              │
                       4. tests                                  │
                       5. policy ←── REFUSE_PATTERNS list        │
                       6. security                               │
                       7. diff sanity                            │
                       8. sandbox                                │
                       9. confidence (≥0.95 if destructive)      │
                            │                                    │
                            └────────────────┬───────────────────┘
                                             ▼
                                    {ok: true|false, reasons:[]}
                                             │
                              ┌──────────────┴──────────────┐
                              │ ok                          │ !ok
                              ▼                             ▼
                          apply                      queue + log lesson
                              │                             │
                              └─────────┬───────────────────┘
                                        ▼
                                outcome-log.py ──→ outcomes.jsonl
```

## 4. The data flywheel (self-improvement)

```
   1k–10k actions/day                    weekly Sun 5am
   ─────────────────                    ──────────────────
   outcomes.jsonl  ─── self-improve.sh sft ────► axentx/surrogate-1-self-traces (HF dataset)
                                                       │
                  ─── self-improve.sh kto ────► axentx/surrogate-1-pref-kto    (HF dataset)
                                                       │
                  ─── self-improve.sh skills ─► axentx/surrogate-1-skills      (HF dataset)
                                                       │
                                                       ▼
                                              kaggle-trainer.sh next round
                                              (V9 picks up these mixed in
                                               via merge_external())
```

## 5. V8 training stack (kaggle-trainer.sh)

| Layer | Technique | Source | Knob |
|---|---|---|---|
| Quant | 4-bit NF4 + double-quant | bitsandbytes | (default) |
| Adapter | LoRA r=64 (alpha=128, dropout=0.05) | peft | `LORA_R=64` |
| Adapter | DoRA decomposition | Liu '24 | (always on) |
| Adapter | RSLoRA scaling | Kalajdzievski '23 | (auto if peft supports) |
| Adapter | **LoRA init mode** *(NEW V8)* — 5 modes: `pissa_niter_4` (default, Meng '24), `loftq` (Li '23), `loftq+pissa` (sequential 2-pass hybrid), `corda` (Yang '24 NeurIPS unified hybrid), `gaussian` | Meng/Li/Yang | `SUR_LORA_INIT=pissa_niter_4` |
| Adapter | Spectrum-lite (top-70% layers) | proxy of Hayou '24 | `SPECTRUM_TOP_FRACTION=0.70` |
| Optimizer | Paged AdamW 8-bit | bnb | (default) |
| Optimizer | **LoRA+ (lr_B = 16·lr_A)** *(NEW V8)* | Hayou '24 | `SUR_LORA_PLUS_RATIO=16` |
| Loss | NEFTune α=5 | Jain '23 | `neftune_noise_alpha=5` |
| Schedule | cosine_with_restarts × 3 | SGDR Loshchilov '17 | (default) |
| Data | 5 sibling datasets (round-robin) | Surrogate harvest | `MAX_SAMPLES` |
| Data | Magpie self-instruct | axentx/surrogate-1-synth-magpie | `MAGPIE_TAKE=10000` |
| Data | **ToolACE 1.5×** *(NEW V8)* | Team-ACE | `TAKE_TOOLACE=8000` |
| Data | **Multi-IaC-Eval 2.0×** *(NEW V8)* | AmazonScience | `TAKE_MULTIIAC=5000` |
| Data | **xLAM-fn-call-60k 1.0×** *(NEW V8)* | Salesforce | `TAKE_XLAM=10000` |
| Data | **ITBench-Trajectories 2.0×** *(NEW V8)* | IBM | `TAKE_ITBENCH=3000` |
| Data | **Code-Feedback 1.0×** *(NEW V8)* | m-a-p | `TAKE_CODEFB=8000` |
| Filter | Active-learning teachable (perplexity middle 50%) | proxy of teachable-prompt | `DISABLE_AL=0`, `AL_SAMPLE_CAP=20000` |
| Phase 2 | **GRPO + execution reward (scaffold)** *(NEW V8, opt-in)* | DeepSeekMath '24 | `RUN_GRPO=1` (req: TRL ≥0.12) |

Hub: `axentx/surrogate-1-7B-v1.2-research`.

## 6. Bench coverage (bench-v1-vs-v15.sh)

| # | Eval | Target | Threshold |
|---|---|---|---|
| 1 | HumanEval+ | code completion | ≥84 |
| 2 | MBPP+ | basic Python | ≥75 |
| 3 | LiveCodeBench v6 | recent + decontaminated | ≥42 |
| 4 | BFCL v3 | function calling | ≥70 |
| 5 | RULER @16K | long context | ≥85 |
| 6 | SWE-Bench Verified lite-100 | agentic real-world | ≥18 |
| 7 | axentx-eval-50 | in-domain DevSecOps | ≥80 |
| 8 | **Multi-IaC-Eval** *(NEW V8)* | CFN+TF+CDK pass-rate via cfn-guard+tfsec | ≥60 |
| 9 | **ITBench-lite** *(NEW V8)* | 102 K8s SRE/CISO/FinOps scenarios | ≥50 |

4-way comparison: v1 vs base7B vs v1.1-extended vs **v1.2-research**.

## 7. Key files (single source of truth)

```
~/.surrogate/hf-space/
├── bin/
│   ├── kaggle-trainer.sh                  ← V8 trainer (heredoc emits train.py)
│   └── v2/
│       ├── verifier-ensemble.py           ← 9-layer safety gate + 14 HardGuards
│       ├── surrogate-call.py              ← strict-JSON LLM call helper
│       ├── outcome-log.py                 ← append outcome JSONL
│       ├── idempotency.py                 ← sha256(plan) ledger w/ TTL
│       ├── autonomous-sre.sh              ← daemon: probe → diagnose → verify → apply
│       ├── autonomous-release.sh          ← daemon: recon → spec → CISC patch → PR
│       ├── self-improve.sh                ← flywheel: outcomes → datasets → trigger
│       ├── watchdog.sh                    ← independent kill-switch observer
│       ├── auto-swap-and-bench.sh         ← post-train: swap LoRA → bench → decide
│       ├── swap-zerogpu-lora.sh           ← LoRA hot-swap on PRO Spaces
│       ├── bench-v1-vs-v15.sh             ← 4-way 9-eval comparison
│       └── post-bench-decide.sh           ← A/B/C dispatcher after bench
└── configs/
    └── v2/
        └── stage1-sft-v1.5-extended.yml   ← axolotl config for branch B/C

~/.surrogate/state/
├── outcomes.jsonl          ← append-only log of every autonomous action
├── idempotency.jsonl       ← sha256 plan ledger
├── queue/<ts>/             ← actions verifier-rejected, awaiting review
├── specs/                  ← release-cycle spec.json + winners
├── repos/                  ← cached clones for autonomous-release PR creation
├── self-improve/           ← built SFT/KTO/skill JSONL files
└── training-queue.log      ← flag file the user checks for "time to retrain"

~/Documents/Obsidian Vault/AI-Hub/knowledge/
├── surrogate-1-autonomous-arch.md         ← THIS FILE (the map)
└── trends-2026/
    ├── coding-llm-frontier.md             ← 433 lines, V8 picks justified
    ├── devsecops-sre-agentic.md           ← 361 lines, datasets + evals
    ├── autonomous-24x7.md                 ← 453 lines, HardGuards + sandbox
    └── self-improvement.md                ← 355 lines, flywheel cron cadence
```

## 8. How to run it

### Bring the daemons up
```bash
# Watchdog FIRST, then daemons (so any kill-switch is armed before risk)
nohup bash ~/.surrogate/hf-space/bin/v2/watchdog.sh \
    > ~/.surrogate/logs/watchdog.log 2>&1 &
sleep 5
nohup bash ~/.surrogate/hf-space/bin/v2/autonomous-sre.sh \
    > ~/.surrogate/logs/autonomous-sre.log 2>&1 &
nohup bash ~/.surrogate/hf-space/bin/v2/autonomous-release.sh \
    > ~/.surrogate/logs/autonomous-release.log 2>&1 &
# self-improve + auto-swap-and-bench already triggered by cron / on-demand
```

### Status check
```bash
bash ~/.surrogate/hf-space/bin/v2/self-improve.sh status
ls ~/.surrogate/state/queue/      # any actions awaiting review?
tail -20 ~/.surrogate/state/outcomes.jsonl | jq -s 'group_by(.outcome) | map({k:.[0].outcome, n:length})'
pgrep -af "autonomous-|watchdog\.sh|auto-swap"
```

### Disarm + reset
```bash
pkill -f autonomous-sre.sh
pkill -f autonomous-release.sh
pkill -f watchdog.sh
rm -f ~/.surrogate/state/watchdog-killed   # re-arm watchdog
```

### Kick V8 training
1. Stop V6/V7 in Kaggle UI
2. Replace `train.py` with `~/Desktop/surrogate-1-train-v8-research.py`
3. Save Version
4. `auto-swap-and-bench.sh` daemon picks up the new adapter automatically

## 9. The non-negotiables (HardGuards, NEVER override in code)

The 14 patterns in `verifier-ensemble.py:REFUSE_PATTERNS` are deterministic
refusals enforced in Python, not the prompt. They cover:

1. `rm -rf /` family + `chmod 777` outside `/tmp` + `chown -R` on system paths
2. `DROP DATABASE/TABLE/SCHEMA`, `DELETE` without `WHERE`, `TRUNCATE`
3. `terraform destroy`, `cdk destroy` on prod, `terraform apply` on prd workspace
4. `aws s3 rb --force`, `ec2 terminate-instances` w/o dry-run, `rds delete` w/o final snapshot, Route53 DELETE
5. `kubectl delete ns`, `kubectl delete *prod*`, helm install from non-allowlisted registry
6. `git push --force` to main/master/prod, `git filter-branch/repo`
7. IAM `Allow * on *`, IAM `Allow Principal *`, destructive ops on admin/root/prod identities, `revoke prod SG`
8. `dd if=/dev/zero` to disk, `iptables -F`
9. `curl|sh` from network, untrusted `npx`
10. AKIA keys, private keys, OpenAI/Anthropic/HF tokens in patch
11. `iam deactivate-mfa-device`, MFA bypass policies
12. `docker pull` without digest pin

Plus: confidence ≥0.95 required if destructive keyword detected.

## 10. Known limits + next steps

- ✅ V8 trainer: 5 research-driven additions (PiSSA, LoRA+, ToolACE, Multi-IaC, Code-Feedback) + GRPO scaffold
- ✅ 9-layer verifier with 14 HardGuards
- ✅ 4 daemons + watchdog operational
- ✅ Self-improvement flywheel writing back to HF datasets
- ⏳ GRPO Phase-2 needs TRL ≥0.12 + ≥30GB VRAM headroom (defer to Civo L40S v2)
- ⏳ Argo Rollouts canary integration (need K8s)
- ⏳ Daytona/E2B sandbox upgrade (currently `docker --network=none`)
- ⏳ Independent watchdog VM with STS-revoke (currently sibling process)
- ⏳ multi-iac-eval.py + itbench-lite.py eval modules (placeholders in bench)
- ⏳ axentx-eval-50.py harness (placeholder)

V9 stretch ladder: 14B base + GRPO Phase-2 if T4×2 stays / Civo L40S 48GB
when ready / Modal H100 burst for distillation cron.
