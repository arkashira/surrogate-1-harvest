# axentx-security-daemon

> ROADMAP-100 #89. Security review pass — OK / SEC-BLOCK.

| Field | Value |
|---|---|
| Role | App-sec review (SQLi/XSS/secret-leak/broken-auth/SSRF/deserialization/race) |
| Stage in pipeline | qa → **security** (parallel with perf) → commit |
| In | `qa-queue/*.json` (also seen by perf-daemon) |
| Out | `commit-queue/*.json` (OK) or `dev-queue/*.json` (SEC-BLOCK with findings) |
| Idempotency | Adds `security_findings` block to item.history |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `SEC_POLL_SEC` | `60` | Poll cycle |

## System prompt summary

"Application security engineer. SEC-BLOCK only on HIGH/CRIT. Lows + meds → OK with note. No-fluff scope: SQLi, XSS, secret leak, broken auth, SSRF, unsafe deserialization, race conditions, IDOR."

## Failure modes

- Borderline finding → OK + flag for next reviewer pass.
- LLM exhaustion → re-queue.
