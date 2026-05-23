# Threat model — surrogate-1 + axentx + hermes

> STRIDE-lite threat model for the autonomous AI dev fleet. Last updated 2026-05-02.

## Assets
| Asset | Sensitivity | Where it lives |
|---|---|---|
| HF Hub training data (9.56 TB) | low (public) | HF Hub |
| Agent decisions (103+ records) | low | swarm-shared/decisions/ + Vectorize |
| LLM API keys (12 providers) | high | Mac, GCP env, CF Worker secrets |
| GitHub PAT (write to axentx org) | high | Mac, GCP env, CF Worker secrets |
| Cursor service auth token | medium | Mac, GCP, CF Worker secret |
| Supabase service key | high | Mac, GCP env |
| Cloudflare API token | high | Mac, GCP env |
| User personal data | none collected | n/a |

## Threats × Mitigations

### Spoofing
- **T**: Forged requests to cursor service
  - **M**: Worker requires X-Auth-Token on all writes (#76 rate-limits abuse too)
- **T**: Auto-bot commits look human-authored
  - **M**: Conventional Commits enforced (#10), CODEOWNERS routing (#8), commit msg includes `axentx-dev-bot`

### Tampering
- **T**: Audit log altered to hide actions
  - **M**: D1 trigger blocks UPDATE/DELETE on audit_log (#77 immutability)
- **T**: Adapter weights replaced on HF Hub
  - **M**: Hub commits are git-signed (HF infra); adapter eval gate (#2) catches unexpected behavior

### Repudiation
- **T**: "I didn't make this commit" disputes
  - **M**: Auto-bot commits attributed to `Hermes (Surrogate-1)` (real email); audit log immutable

### Information disclosure
- **T**: Secrets leaked in committed code
  - **M**: gitleaks-style scanner pre-push (#74); GitHub secret scanning enabled
- **T**: PII in training data
  - **M**: PII scrubber (#52) runs on every pair before HF Hub push
- **T**: Agent decisions leaked publicly
  - **M**: Decisions are intentionally public (model improvement signal) — not a threat

### Denial of service
- **T**: Cursor service hammered by malicious clients
  - **M**: Per-IP rate limit (#76) — 100/min/IP
- **T**: LLM API quota exhausted
  - **M**: 12-provider fallback chain + per-provider 60s cooldown after 429
- **T**: GCP e2-micro OOM
  - **M**: MemoryMax per daemon, RSS soft cap with graceful exit, self-heal restart

### Elevation of privilege
- **T**: Agent gains write access to repos it shouldn't touch
  - **M**: Each daemon runs as `ubuntu` (non-root) on GCP; PAT scope is org-wide but auto-bot commits go through CODEOWNERS; branch protection (#3) requires status checks

## Out of scope
- Physical security (cloud free tier, no on-prem)
- Insider threat from team members (single-person operation)
- Nation-state APT (not in our threat model — different risk class)

## Open issues
- AI Gateway scope still missing → can't enforce per-provider rate limits centrally
- R2 not enabled → backups go to HF Hub instead (acceptable, not as private)
- Workers Pipelines forbidden (beta access maybe) → no native ETL pipeline yet
