---
name: metrics-schema-collection
description: Define and collect training metrics via a YAML schema and persist them to a SQLite table.
tags: [metrics, yaml, sqlite, observability]
---

## Overview
This skill provides a pattern for adding a structured metric collection schema to a data pipeline, enabling consistent logging and analysis of training runs.

## Steps
1. **Create YAML Schema**
   - Path: `metrics/training_metrics.yaml`
   - Define metric keys (e.g., `train_time_seconds`, `steps_per_second`, `loss_avg`, `accuracy`).
2. **Create SQLite Table**
   - Table name: `training_metrics`
   - Columns correspond to the YAML keys (use appropriate types: INTEGER, REAL, TEXT).
3. **Update Data Pipeline**
   - In the data preparation script (e.g., `scripts/prepare_data.py`), load the YAML schema.
   - At the end of each training epoch, write a row to `training_metrics`.
4. **Visualization**
   - Use any SQLite client or charting library to query and plot metrics over time.

## Pitfalls & Tips
- Ensure the SQLite write operations are serialized (e.g., using a mutex) to avoid lock contention (see Netmaker SQLite pattern).
- Validate metric values before insertion to prevent corrupt rows.
- Keep the YAML schema versioned alongside code for reproducibility.

## Tags
#metrics #yaml #sqlite #observability #training
