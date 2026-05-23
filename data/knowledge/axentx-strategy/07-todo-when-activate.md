---
title: Activation Checklist (when trigger fires)
tags: [checklist, activation, todo]
last_updated: 2026-04-18
---

# Activation Checklist — When Trigger Signal Fires

> Don't use this yet. Triggers not met as of 2026-04-18.
> See `05-paths-and-triggers.md` for trigger definitions.

## Phase 0: Pre-activation (1-2 weeks)

### IP protection (DO FIRST, regardless of path)
- [ ] Run `git log --all` on all axentx repos, save output
- [ ] Timestamp proof: email to personal account, save PDF
- [ ] Push to personal private GitHub if not already
- [ ] Backup full codebase to external drive (offline)
- [ ] Document original creation dates + evidence of off-hours work
- [ ] Print + sign IP inventory document

### Personal readiness
- [ ] Health check (sleep, stress, physical)
- [ ] Finance check (6+ months savings runway)
- [ ] Family/partner alignment
- [ ] Company 1 grief status (baseline or processed, not reactivated)
- [ ] Day job performance at quit-ready level (no pending disciplinary)

### Context prep
- [ ] Re-read all files in `~/Documents/Obsidian Vault/AI-Hub/knowledge/axentx-strategy/`
- [ ] Update any outdated sections based on new info
- [ ] Verify Claude context current (run through chat review)

## Phase 1: Path A activation (if boss offers fair term)

- [ ] Receive formal written offer (MOU, not verbal)
- [ ] Review terms against `04-negotiation-strategy.md` checklist
- [ ] Legal review (Thai labor law + IP ownership) — budget 20-50k THB
- [ ] Negotiate specific sticking points
- [ ] Counter-propose with:
  - Revenue share formula + audit rights
  - IP ownership clarity (pre-existing vs new work)
  - Offload current duties (hire 1 DevOps backfill)
  - Timeline commitments (Y1 milestones)
- [ ] Sign MOU
- [ ] Stage 3 reveal: share codebase + specs with team
- [ ] Onboard น้อง DR (or other product team members)
- [ ] Begin product development sprint planning

## Phase 2: Path E activation (if going solo)

### Legal/Corporate (weeks 1-4)
- [ ] Resign from current role (1-3 months notice)
- [ ] Incorporate company (Ltd or Partnership)
  - Name: TBD (suggestions: Ashira Technology, axentx Co., Ltd., Fuseship, etc.)
  - Registered capital: 1M THB (minimum for VC-friendly)
  - Shareholders: Ashira 100% initial, ESOP pool 10-15%
- [ ] VAT registration (ภ.พ. 30)
- [ ] Accountant hire (or freelance 20k/month)
- [ ] Bank account (corporate)
- [ ] Trademark filing (Arkship, Surrogate-1, Costinel, Vanguard)
- [ ] Draft employment contracts + NDA template
- [ ] Domain registrations (arkship.io / axentx.com / etc.)

### Team assembly (weeks 2-8)
- [ ] Reach out to ex-Company 1 team — gauge interest
- [ ] Job description: Senior Full-stack (Python/Go/TypeScript)
- [ ] Interview + hire 2 engineers
- [ ] Job description: Biz Ops (sales + finance + procurement)
- [ ] Interview + hire 1 biz ops
- [ ] Set up advisor agreements (boss, KX alum, others)
- [ ] Culture doc + operating principles

### Funding (parallel, weeks 2-12)
- [ ] NIA IDE application draft
- [ ] NIA MVP Grant application
- [ ] depa Digital Transformation Fund inquiry
- [ ] Pitch deck v1 (for investors, ≠ 1-pager for boss)
- [ ] Update LinkedIn + KX alumni directory presence
- [ ] Book 5 warm coffee chats via KX network

### Product (weeks 4-24)
- [ ] Code audit: which modules production-ready vs throwaway
- [ ] Stack decision: keep Python FastAPI or rewrite Go?
- [ ] DevOps: CI/CD, staging, monitoring infra
- [ ] Documentation overhaul (public README for OSS bait products)
- [ ] Security audit (before first customer deployment)
- [ ] Compliance prep (ISO 27001? PDPA? depends on gov customers)

### Go-to-market (weeks 8-24)
- [ ] Consulting pipeline: reactivate 10M-deal network
- [ ] Close 1 consulting deal (3-5M revenue)
- [ ] Use consulting revenue to fund product dev
- [ ] Pilot customer: RID-type agency via boss intro OR KX network
- [ ] Hardware vendor partnership: Huawei first (easiest)
- [ ] First OSS release (Costinel or Vanguard, whichever more stable)
- [ ] Community building: Thai DevOps Meetup talks, LinkedIn content

## Phase 3: Growth (year 2-3)

To be detailed closer to time. Key milestones:
- [ ] 3-4 paying customers
- [ ] ARR 20M+ THB
- [ ] Team 6-10
- [ ] Surrogate-1 fine-tune complete (Qwen3.5-Coder-14B + LoRA)
- [ ] Hardware vendor premier partner status
- [ ] Optional: seed round 20-50M
- [ ] ASEAN expansion scoping

## Emergency / abort triggers

If ANY of these during activation:
- Health crisis (self or family)
- Finance crisis
- Company 1 grief reactivates
- Key hire quits within 90 days
- First customer deal falls through + no backup
- NIA grant rejected + no alternative

→ **Pause, reassess, possibly rollback to Path D**

Not all setbacks = abort. Use judgment. Key question: "is this fixable with time, or structural?"

## Files to reference during activation

- This file (checklist)
- `00-overview.md` (strategic context)
- `02-product-vision.md` (for pitch deck)
- `03-business-model.md` (for investor questions)
- `04-negotiation-strategy.md` (for boss / legal)
- `~/axentx/` (actual code)
- `~/.claude/bin/` (tooling to port)
