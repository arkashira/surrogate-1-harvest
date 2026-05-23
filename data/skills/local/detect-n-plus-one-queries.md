---
name: detect-n-plus-one-queries
description: Detect and optimize N+1 query patterns in codebases, especially in async database access layers. Provides guidelines to consolidate queries, use eager loading, and add caching to reduce DB round-trips.
version: 1.0.0
author: Hermes Agent
---

# Detect N+1 Queries

1. Identify loops that issue DB calls inside iteration (e.g., `for ...: await db.execute(...)`).
2. Use tools like `sqlalchemy`'s `selectinload` or ORM eager loading to fetch related data in a single query.
3. Consolidate per-group queries into aggregated queries with `GROUP BY` or `JOIN`.
4. Cache recent results (e.g., last day's cost data) using in‑memory structures or external cache (Redis) to avoid repeat scans.

# Example Fix
```python
# Before (N+1 pattern)
for account in accounts:
    for service in services:
        cost = await db.execute(select(DailyCost).where(...))
        # process cost
```
```python
# After (single query with join)
stmt = (
    select(DailyCost.account_id, DailyCost.service, func.sum(DailyCost.cost_usd).label('total'))
    .where(DailyCost.date >= start, DailyCost.date <= end)
    .group_by(DailyCost.account_id, DailyCost.service)
)
results = await db.execute(stmt)
# process aggregated results
```

# Caching
- Store recent aggregated results in a dict keyed by `(account_id, service)`.
- Invalidate cache when new data is ingested.

# Pitfalls
- Ensure cache consistency on data updates.
- Beware of stale data causing missed anomalies.

# Verification
- Run performance benchmark before/after changes; expect DB round‑trip reduction.
- Monitor query count via DB logs.
