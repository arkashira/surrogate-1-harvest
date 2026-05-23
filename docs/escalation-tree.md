# Escalation tree (free-tier alternative to PagerDuty)

```
alert source                     destination                  next-step
─────────────────────────────── ──────────────────────────── ─────────────
watchdog sweep error  ──────→  Discord webhook              auto-cooldown 5m
self-heal restart     ──────→  Discord webhook              auto-ack
GCP daemon dead       ──────→  Discord webhook              self-heal triggers within 60s
Supabase paused 7d    ──────→  Discord webhook              manual: re-activate or migrate
HF Space OOM          ──────→  Discord webhook              user → factory rebuild
GitHub action fail    ──────→  Discord webhook              user → debug
Kaggle 12hr wall      ──────→  Discord webhook              auto-resume next account
                                                            (V18b checkpoint flow)

ALL above hit Discord first. If unattended:
  +1 hour    → no further action (single-person)
  +6 hours   → consider WhatsApp/Telegram bridge (TBD)
  +24 hours  → manual sweep at next active window
```

## Tools used (all free)
- Discord: primary alert channel
- Worker cron: `*/5` health probe (already shipped)
- Watchdog daemon on GCP: secondary, 5-min sweep
- Self-heal daemon: auto-restart for known dead-service patterns

## What we DON'T have (gaps for future)
- SMS / phone call escalation (paid)
- Multi-channel routing per severity
- Acknowledgement tracking (alerts repeat until acked)

## Tracking via D1
Add `alerts` table:
| col | type | note |
|---|---|---|
| id | int auto | |
| source | text | watchdog/self-heal/etc |
| severity | text | P0-P3 |
| message | text | first 200c |
| acked_at | int | unix ts; NULL = unacked |
| created_at | int | |

Worker route `/alerts/ack/<id>` accepts ack via auth header. UI on `/dash` lists unacked.
