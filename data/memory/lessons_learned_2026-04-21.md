## 2026-04-21: Override Analytics Schema and ETL Improvements
- Context: Added proposal for Costinel analytics view enhancements during data pipeline run.
- Insight: Need explicit timestamps, NOT NULL defaults, indexes, and materialized view refresh for analytics performance.
- Fix/Pattern: Define materialized view with derived disagreement rate, schedule nightly REFRESH CONCURRENTLY, add indexes.
- Prevention: Ensure future analytics tables include timestamps and defaults at creation.
- Tags: devops analytics data-warehouse
