---
name: region-column-cloud-provider
description: Add a region column to cloud provider tables and update related metrics for region-level cost analysis.
version: 1.0.0
author: Ashira
platforms: [macos, linux]
metadata:
  hermes:
    tags: [data-engineering, schema, metrics, cost-governance]
---

# Overview
This skill guides adding a `region` column to cloud provider tables (e.g., `cloud_providers`) and updating ETL pipelines and analytics to enable region‑level cost metrics.

# Steps
1. **Schema Migration**
   ```sql
   ALTER TABLE cloud_providers ADD COLUMN region VARCHAR(64) NOT NULL DEFAULT 'global';
   ```
2. **Backfill Existing Data**
   - Populate `region` for existing rows based on provider configuration or default to `'global'`.
3. **ETL Update**
   - Ensure data ingestion pipelines capture the `region` field and propagate it to downstream stores.
4. **Metric Definition**
   - Create a new metric `cost_by_region` that aggregates spend per region:
   ```sql
   SELECT region, SUM(cost) AS total_cost FROM cloud_costs GROUP BY region;
   ```
5. **Dashboard Update**
   - Add visualizations for `cost_by_region` to the analytics UI.

# Pitfalls & Checks
- **Schema Validation**: As noted in the knowledge graph, adding fields without a default can cause JSON Schema validator rejections. Use a default value to maintain compatibility.
- **Semantic Layer**: Unrecognised fields may trigger parse failures. Ensure the new `region` field is declared in any semantic model definitions.
- **Backfill**: Verify all existing rows receive a valid region to avoid nulls in metrics.

# Verification
- Run `SELECT COUNT(*) FROM cloud_providers WHERE region IS NULL;` – should return `0`.
- Confirm the new metric appears on the dashboard and matches manual aggregation.

# Tags
`#data-engineering #schema #metrics #cost-governance`
