---
name: Code Style & Engineering Practices
description: Write human-like code using design patterns, data-driven approach, type safety, Result pattern, guard clauses
type: feedback
---

Write code like a senior engineer at Google/Stripe/Netflix.

**Why:** User explicitly wants production-quality code with modern engineering practices, not AI-style verbose code.

**How to apply:**
- No comments unless logic is genuinely non-obvious
- Guard clauses at top, happy path flows down
- Result pattern `{ok, data} | {ok: false, error}` over throwing
- Data-driven: table-driven logic, config over code, schema-driven validation
- Design patterns: Repository, Factory, Strategy, Builder, Circuit Breaker
- Type safety: branded types, discriminated unions, parse don't validate, Zod/Pydantic at boundaries
- Naming: intent-revealing, verb conventions (get/fetch/compute/ensure/parse), units in names
- Resilience: retry+backoff+jitter, circuit breaker, timeout budgets, graceful shutdown
- Observability: structured JSON logs, correlation IDs, RED/USE metrics
- Testing: factory-based test data, mock boundaries only, property-based for edge cases
- Security: allowlist validation, default-deny, fail-closed, parameterized queries
- Small PRs (< 400 lines), one concern per changeset


---

**Graph**: [[../Documents/Obsidian Vault/AI-Hub/patterns/MOC|🧭 Graph Hub]] · [[MEMORY|Memory Index]] · [[knowledge_index|Pattern Index]] · [[lessons_learned|Lessons]]
