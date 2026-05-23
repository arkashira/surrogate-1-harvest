# axentx-support-inbox-daemon

> ROADMAP-100 #89. Customer-support inbox → Discord forwarder.

| Field | Value |
|---|---|
| Role | Polls a webhook for new tickets, forwards to Discord support channel |
| Stage in pipeline | Side-channel (no internal queue) |
| In | `GET {SUPPORT_INBOX_URL}?since=<ISO8601>` returning `{tickets: [...]}` |
| Out | Discord webhook post per ticket + `state/.support-inbox-seen.json` |
| Idempotency | `seen_ids` file persisted; restart-safe |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `SUPPORT_INBOX_URL` | `https://axentx-support.workers.dev/inbox` | Source endpoint |
| `SUPPORT_INBOX_TOKEN` | — | Bearer auth (optional) |
| `DISCORD_WEBHOOK` | — | Sink |

## Endpoint contract (caller wires this)

```
GET {SUPPORT_INBOX_URL}?since=<ISO8601>
  Auth: X-Auth-Token: <SUPPORT_INBOX_TOKEN>
→ 200 {"tickets": [{id, received_at, from, subject, body}]}
```

## Failure modes

- 404 (endpoint not deployed yet) → no-op, retry next cycle.
- Empty tickets → no-op.
- Discord send fails → keep id out of seen set, retry next cycle.
