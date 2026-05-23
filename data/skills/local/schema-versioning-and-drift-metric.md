---
name: schema-versioning-and-drift-metric
description: Introduce schema versioning and a drift metric to track data conformity across schema changes.
category: data-engineering
tags: [schema, versioning, data-quality, metric]
---

# Overview
Data pipelines evolve, and schemas change. Without explicit version tracking, downstream consumers may encounter mismatched data structures. This skill adds a `version` field to the canonical schema definition and tracks the **schema_version_drift** metric, which measures the proportion of records whose schema version diverges from the current canonical version.

# Steps
1. **Update Canonical Schema**
   - Edit `data/schema/schema.yaml` and add a top‑level `version: 1` field (increment for each breaking change).
2. **Embed Version in Records**
   - Modify data ingestion scripts to include `metadata.schema_version` matching the current schema version when writing JSONL records.
3. **Validate Schema Version**
   - Extend `scripts/validate_schema.py`:
     - Load `schema.yaml` and extract its `version`.
     - For each record, compare `metadata.schema_version` to the canonical version.
4. **Compute Drift Metric**
   - During validation, count records with mismatched versions.
   - At the end, calculate `drift = mismatched / total * 100`.
   - Write the result to `metrics/schema_version_drift.yaml`:
     ```yaml
     schema_version: 1
     drift_percentage: 2.3
     timestamp: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
     ```
5. **CI Integration**
   - Add a CI step that fails if `drift_percentage` exceeds a threshold (e.g., 5%).
6. **Documentation**
   - Update `README.md` with a "Schema Versioning" section describing the process and the metric.

# Pitfalls
- **Forgot to bump version**: Ensure any breaking change to `schema.yaml` increments the `version` field.
- **Missing metadata**: Legacy records without `metadata.schema_version` will be counted as drift; back‑fill them during migration.
- **Performance**: Validation on large datasets can be slow; consider streaming validation or sampling for the metric.

# Verification
- Run `scripts/validate_schema.py` on a sample dataset; confirm `metrics/schema_version_drift.yaml` is generated and the `drift_percentage` is reasonable.
- Check CI reports for the new metric and any failures.
