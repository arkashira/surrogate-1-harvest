---
name: architecture-decision-record
description: Document architecture decisions in ADR format with context, decision rationale, and consequences
version: 1.0.0
author: HermesSynthesizer
tags: ["architecture", "adr", "documentation", "devops"]
created_at: 2026-04-22T11:12:21.548160
---

# Architecture Decision Record

## Rationale
Pattern appeared 3 times (event-driven microservices, anomaly detection service, SOC2 collector)—consistent Context/Decision/Consequences structure. Standard ADR format reusable for any architecture work.

## Steps
1. Capture context (current state, constraints, requirements driving the decision)
2. State the decision clearly (what architecture/technology/pattern chosen)
3. List positive consequences (scalability, maintainability, performance gains)
4. List negative consequences (complexity, cost, operational overhead)
5. Define mitigations for negatives (managed services, observability, team training)
