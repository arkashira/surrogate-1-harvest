---
title: axentx Product Vision & Ecosystem
tags: [axentx, product, architecture, competitive]
last_updated: 2026-04-18
---

# axentx Ecosystem — Product Vision

## Core thesis

**"AI-native Platform Engineering Suite ที่ break ก่อน cost/security ไหล + แปลง intent เป็น IaC ให้ dev ไม่ต้องรู้ infra + AI ตัดสินใจได้บางระดับแต่ human-in-the-loop เสมอ"**

## 6-Product map

### 1. Arkship (commercial core)
- **Role**: Platform decision engine + Intent Language compiler + Case management
- **User**: Devs (primary, including vendors delivering to gov), gov officers (secondary, for audit)
- **Pain solved**: Devs อยาก service + ไม่รู้ infra detail + vendor ทำ scope ผิด + gov ต้อง audit manually
- **Features**:
  - Drag-drop UI (Port.io-style) OR simple YAML (CloudFormation-like but easier)
  - YAML → Terraform/CloudFormation/Pulumi behind scenes
  - Surrogate-1 review (best practice, suggest missing components)
  - Tier-based approval (Tier 0/1/2/Forbidden + GOD MODE)
  - Workflow orchestration (Temporal)
  - Vendor scope check (new, unique: detect over/under-provisioning)

### 2. Surrogate-1 (commercial core, "ลูกรัก")
- **Role**: AI brain with 6 roles: Guardian, Navigator, Assembler, Sherlock, Auditor, Coach
- **Current**: Qwen2.5-Coder:7B + prompt (placeholder)
- **Plan**: GLM-5 744B MoE → Huawei ModelArts OR Qwen3.5-Coder-14B + LoRA (pragmatic option)
- **Knowledge**: 15 domains (DevSecOps, SRE, Platform, SOC, FinOps, DataOps, MLOps, AIOps, etc.)
- **NO OSS** — commercial only, this is the moat

### 3. Costinel (OSS bait)
- **Role**: FinOps — Sense + Signal ≠ Execute
- **Status**: v4.2 เคย deploy แล้ว (ปล่อยหมดอายุ)
- **Features**:
  - Multi-cloud cost dashboard
  - Cost center → project code → mandatory tag (via Arkship integration)
  - PM integration (Jira/ClickUp/Asana → ticket → cost allocation)
  - Anomaly detection + narrative (who opened what when — saves CloudTrail dive)
  - Rightsizing recommendations with push notifications
  - Exec report generation
  - Multi-channel notify (Slack/Discord/Teams/Email/LINE)
  - Integrated with Arkship for auto-remediation flow

### 4. Vanguard (OSS bait)
- **Role**: SOC + Cloud Security Assessment
- **Stack**: Prowler + Cloud Custodian + Cartography + ScoutSuite + Steampipe + Kube-bench
- **DB**: Neo4j (asset graph), Postgres (findings), Redis (queue)
- **Features**: CSPM, compliance frameworks, auto-remediation via Custodian
- **Integration**: findings → Arkship case → Terraform fix proposal

### 5. AxiomOps (pre-cursor, redundant now)
- Originally Surrogate System (Digital Operator concept)
- Stack: Planner (Qwen 7B) + Reviewer (Claude) + Policy Gate (OPA) + Executors
- **Decision**: Consolidate into Arkship — don't deploy separately

### 6. Workio (unrelated)
- LINE-based time tracking (multi-tenant, different market)
- Not part of axentx commercial suite
- May be separate venture

## Flow diagram

```
Dev (vendor) ─┐
              ├─→ Arkship UI (drag-drop / YAML-simple)
Gov officer ──┘         │
                        ├─→ Intent Language Compiler → Terraform / CF / Pulumi
                        │         │
                        │         ├─→ Surrogate-1: best practice review
                        │         ├─→ Vanguard: security scan  (pre-deploy)
                        │         └─→ Costinel: cost forecast   (pre-deploy)
                        │                    │
                        │         ┌──────────┘
                        │         ▼
                        └─→ Tier-based approval (dev / lead / gov officer)
                                  │
                                  ├─→ Execute (within tier): terraform apply
                                  ├─→ Hardware recommend → Dell/HPE/Huawei API
                                  └─→ Deploy → Monitor
                                                │
                                  ┌─────────────┘
                                  ▼
                        Runtime: Costinel + Vanguard observe
                                  │
                                  ├─ Anomaly → Arkship case
                                  │         ├─→ Tier approval
                                  │         └─→ Auto-remediate (within tier)
                                  └─ Audit log + Exec report
```

## Competitive positioning

| Player | Lane | Gap vs axentx |
|---|---|---|
| OpsTella (Opsta) | Consulting + Backstage extended | Upstream lock, no AI-native |
| Humanitec | EU enterprise IDP | Score format only, no IaC export |
| Port.io | Dev portal drag-drop | No IaC, no AI advisor |
| Backstage | Spotify dev portal | Big-tech only, no automation |
| Crossplane | K8s-native IaC | Infra-only, no business context |
| AWS Control Tower | AWS-only | Single cloud, no Thai gov fit |
| Vantage / CloudZero | FinOps-only | No IaC, no security |
| Wiz / Orca | Cloud security | Security-only, no FinOps |

**axentx unique combination**: Thai-native + multi-cloud + on-prem ready + AI-first + integrated FinOps+SOC+IaC + gov-aware + tier-based AI authority

## Critical features for gov market

1. **On-prem first** (geopolitics: Iran-US tensions, data sovereignty)
2. **Vendor accountability** (detect over/under scope automatic)
3. **Audit trail + Exec report** (OAG compliance)
4. **Tier approval** (never auto for gov, only recommend)
5. **Multi-site deployment** (RID-style geographic distribution)
6. **Hardware vendor partnership** (Dell/HPE/Huawei/Lenovo co-sell)
