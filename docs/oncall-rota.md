# On-call rota

## Single-person fail-safe
We are a 1-person operation. Rota = "active" weeks vs "deferred" weeks.

| Week | Owner | Active hours | Auto-defer |
|---|---|---|---|
| Mon-Sun | Ashira | 09:00-22:00 ICT | All other times → Discord queue, ack on next active hour |

## Pages today
- Discord webhook (Hermes channel) catches all alerts
- watchdog spam protection: alert-once + 5-min cooldown
- self-heal-daemon auto-restarts dead services without paging

## Severity ladder
- **P0** (data loss / public service down): immediate, even off-hours
- **P1** (degraded service): next active hour
- **P2** (background failure / training loss): morning standup
- **P3** (informational): Discord backlog, no action

## Auto-acknowledge
- watchdog "stage=SLEEPING" alerts on HF Spaces → SELF-ACK (Spaces auto-restart)
- self-heal "service restarted ok" → SELF-ACK
- Pipeline "agent attempt 3 → forced approve" → SELF-ACK

## Manual escalation
- For sustained P0: tag @Ashira directly in Discord, then SMS via webhook (TBD)
