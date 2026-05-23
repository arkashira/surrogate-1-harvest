---
name: add-timestamp-index-scan-metrics
description: Add a timestamp column and index to a metrics table for improved time‑based queries and performance.
version: 1.0.0
author: Ashira
---

# Pattern: Add timestamp and index to metrics table

## Context
Often metric tables lack a temporal column, making it difficult to query recent data efficiently. Adding a `recorded_at` timestamp with a default value and indexing it enables fast time‑range queries and aligns with best practices for normalization and indexing.

## Steps
1. **Create Alembic migration**
   ```python
   from alembic import op
   import sqlalchemy as sa

   def upgrade():
       op.add_column('scan_metrics', sa.Column('recorded_at', sa.TIMESTAMP(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')))
       op.create_index('ix_scan_metrics_recorded_at', 'scan_metrics', ['recorded_at'])

   def downgrade():
       op.drop_index('ix_scan_metrics_recorded_at', table_name='scan_metrics')
       op.drop_column('scan_metrics', 'recorded_at')
   ```
2. **Run migration**: `alembic upgrade head`
3. **Verify**: Query recent metrics:
   ```sql
   SELECT * FROM scan_metrics WHERE recorded_at >= NOW() - INTERVAL '7 days';
   ```
4. **Optional**: If data volume grows, consider **partitioning** the table by month:
   ```sql
   CREATE TABLE scan_metrics_2026_04 PARTITION OF scan_metrics FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
   ```

## Pitfalls
- Ensure existing rows have a valid timestamp; the default will fill `CURRENT_TIMESTAMP` for new rows, but you may need to backfill for historic data.
- Adding an index can lock the table; schedule migration during low‑traffic windows.

## Verification
- Confirm `recorded_at` column exists: `\d scan_metrics`
- Ensure index is present: `\di+ ix_scan_metrics_recorded_at`
- Run a benchmark query before and after to see performance improvement.

## Tags
#data-engineering #schema-improvement #metrics #timestamp #index
