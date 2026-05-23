---
name: monthly-cost-metric-improvement
description: Add a timestamp column and index for cost metrics to enable efficient time-range queries and monthly aggregation.
tags: [data, metrics, schema, cost-governance]
---
# Monthly Cost Metric Improvement

## Context
Project: Costinel (cloud cost governance platform). Need to track monthly cost per cost center.

## Proposed Change
- Add `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` column to `cost_centers` table.
- Add index on `(cost_center_id, created_at)`.
- Define metric `monthly_cost_per_center` using SQL query to sum costs per month.

## Implementation Steps
1. Run SQL migration:
   ```sql
   ALTER TABLE cost_centers ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
   CREATE INDEX idx_cost_center_created_at ON cost_centers (cost_center_id, created_at);
   ```
2. Create metric view or query as needed.

## Benefits
- Enables month-over-month cost analysis.
- Supports budgeting, forecasting, and anomaly detection.
- Minimal storage overhead.

## Risks & Mitigations
- Slight increase in storage; mitigated by default column.
- Index maintenance overhead; acceptable for typical write volume.

## Reusability
Applicable to any system needing time-bound aggregation metrics.
