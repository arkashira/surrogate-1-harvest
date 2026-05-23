---
name: IAM Orchestration Spec
description: Template for creating Integrated IAM Orchestration specifications aligned with Zero Trust principles.
tags:
  - devops
  - iam
  - zero-trust
  - spec
---
# IAM Orchestration Specification Template

## Problem
Fragmented IAM management across multi‑cloud environments leads to misconfigurations and hampers Zero Trust enforcement.

## User
Cloud security engineers and platform operators responsible for identity lifecycle and access control.

## Solution
Develop an integrated IAM orchestration platform that centralizes identity lifecycle management, policy definition, and enforcement across cloud providers, aligned with Zero Trust principles.

## Acceptance Criteria
- Supports onboarding/offboarding identities across AWS, GCP, Azure via UI/API.
- Policies defined centrally are automatically enforced.
- Zero‑Trust checks applied to every request.
- Audit logs generated for all actions.

## Risks
- Integration complexity across providers.
- Policy conflicts and performance overhead.
- Data sync latency.
- Security of the centralized platform.

> Pattern sourced from internal backlog item `Integrated IAM Orchestration with Zero Trust Frameworks` (Vanguard) and referenced in `Vanguard/AGENTS.md` and `Costinel/AGENTS.md`.
