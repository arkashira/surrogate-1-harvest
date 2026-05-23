---
title: Boss Negotiation — Sequential Disclosure Strategy
tags: [negotiation, boss, disclosure, stages]
sensitivity: confidential
last_updated: 2026-04-18
---

# Negotiation Strategy with Boss

## Current state

- Boss แง้มเรื่อง product team partnership + revenue share (2 ครั้งแล้ว)
- Boss ไม่รู้ว่า axentx exist
- Boss รู้ว่า Ashira เก่งเรื่อง infra, ไว้ใจมาก
- Team candidate: Ashira + น้อง DR (AI) + boss (domain/sales)
- **Status**: Stage 1 active (ฟิวส์เล่นอยู่แล้ว, ยังไม่เปิดไพ่)

## Sequential disclosure — 3 stages

### Stage 1: Vision-only pitch (CURRENT)
- **Framing**: "ผมคิดเรื่องนี้มานานแล้ว, เห็นตลาดมี gap"
- **Show**: Problem statement, market analysis, 3-product concept, business model
- **HIDE**: Working code, screenshots, Kiro specs, directory structure
- **Goal**: Boss react + commit principles (role, %, resources)

### Stage 2: Prototype hint (only after Stage 1 commits)
- **Framing**: "ผมพอมี sketch/prototype จากตอน evaluate Backstage/Port.io ปี 2024-2025"
- **Show**: Architecture diagram, 1-2 UI screenshots
- **Emphasize**: "ของใช้พื้นฐาน, ยังไม่ production ready"
- **Goal**: เพิ่ม credibility หลัง term negotiated

### Stage 3: Full reveal (only AFTER MOU sign)
- **Show**: Full codebase, Kiro specs, RAG infrastructure
- **Goal**: Execute, ไม่ใช่ negotiate

## Why sequential?

**Information asymmetry = negotiation power**
- Boss's info: ต้องการ product, มี channel, ไว้ใจ Ashira, มี budget
- Ashira's info: vision + codebase + clean IP + tested prototype
- ปล่อยข้อมูลก่อน close term = เสียเปรียบ

**Thailand reality**: เมื่อ employer พบ valuable IP ของ employee → default ใช่ "company IP" ไม่ใช่ "employee IP, pay fair"

## Terms to nail before investing more effort

| Term | Ideal | Minimum acceptable | Walk-away |
|---|---|---|---|
| Pre-existing IP | Ashira own, license exclusive | Assign with buyback | Full assign |
| Revenue share % | 20-30% net profit | 10-15% gross | <5% |
| Role/Title | Co-founder / CTO of product | Tech Lead + bonus | Employee + promo |
| Equity | 15-25% if spin-off | Phantom stock | Bonus only |
| Company commitment | Budget, 2-3 engineers hired | 1 engineer + contractor | Just "help when possible" |
| Timeline | 18-month milestone | 12-month pilot | <6 months |
| Exit clause | IP returns if project sunset | Co-ownership | IP stays company |
| Non-compete | Narrow + ≤12 months | ≤18 months | ≥24 months |
| Offload current duties | Release planning off Ashira | Partial offload | No change |

## Red flags — decline product offer if

- "เอา Ashira ไปช่วย product ก่อน, term ค่อยคุย" (scope creep trap)
- "axentx ทำตอน employee, ของบริษัท" (IP grab attempt)
- "มาทำ salary+bonus ดีกว่า" (partnership mask)
- "น้อง DR จะเป็น tech lead" (junior position)
- Verbal only, no written commitment within 3 months

## Green flags — proceed if

- Formal written MOU within 4-8 weeks of formal pitch
- Specific budget + headcount commitment
- Offload current day-job responsibilities (hire backfill)
- Revenue share written + formula clear
- Board seat OR advisor title OR co-founder designation

## IP protection — DO NOW (before Stage 1 pitch)

1. Run `git log --all --format="%h %ai %s"` on all axentx repos
2. Screenshot, save as PDF
3. Email to personal account (timestamp fixed)
4. If local only → push to personal private GitHub now
5. **DO NOT**: force push, rewrite history, delete commits, move between repos
6. Keep commit chain of custody intact

## 1-pager for Stage 1 (TODO)

Short, vision-level, 1 page max:
- Problem: Thai gov IT delivery crisis (vendor scope creep, audit burden, security debt)
- Solution: axentx suite (brief 3-product outline)
- Market: TAM 80-120B THB Thai gov IT spend
- Business model: open-core + hardware partner + SaaS
- Ask: role, %, resources, timeline
- **DO NOT include**: code, specs, screenshots, directory structure

## Alternative if boss hedges / vague

Path D activate:
- Keep codebase sealed
- Continue ~/.claude setup
- Don't invest more time in axentx
- Re-evaluate Q1 next year

## Alternative if deal structure unfavorable

Path E activate:
- Polite decline product role
- Consider own company (software house 2.0 + product)
- Use NIA grant path
- Keep Ashira-boss relationship at employee/friendly level
