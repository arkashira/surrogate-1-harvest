# Single secret store

Currently secrets live in 3 places — this doc consolidates and explains.

## Sources of truth (canonical)
1. **Mac**: `~/.hermes/.env` (chmod 600, ashira:staff)
2. **GCP daemons**: `/etc/surrogate-coordinator.env` (root:root, 600)
3. **CF Worker**: secrets bound via `wrangler secret put` or API PUT
4. **HF Spaces**: secrets bound via Space Settings → Secrets
5. **Kaggle Secrets**: Add-ons → Secrets per kernel

## Sync flow (one-way: Mac → everything else)
```bash
# Mac is canonical. Sync to GCP after rotation:
gcloud compute ssh surrogate-watchdog --zone=us-central1-a \
  --command="sudo cp /tmp/new-env /etc/surrogate-coordinator.env"

# Sync to CF Worker:
for k in HF_TOKEN GROQ_API_KEY ...; do
    v=$(grep "^$k=" ~/.hermes/.env | cut -d= -f2-)
    curl -X PUT -H "Authorization: Bearer $CF_TOKEN" \
      -d "{\"name\":\"$k\",\"text\":\"$v\",\"type\":\"secret_text\"}" \
      "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/scripts/surrogate-1-cursor/secrets"
done

# HF Spaces / Kaggle: web UI only (no API for Space-secrets)
```

## Rotation calendar
See `data/secret-rotation.json` (auto-checked daily by `axentx-secret-watchdog.sh`).

## Future (when scale demands)
- Doppler / Infisical (free tier exists for Doppler)
- 1Password CLI (paid, but $3/mo team)
- HashiCorp Vault (overkill for solo)
- For now: plain dotfiles + manual rotation = good enough

## What's stored where (audit table)
| Secret | Mac | GCP | CF | HF | Kaggle |
|---|---|---|---|---|---|
| HF_TOKEN | ✓ | ✓ | ✓ | — | ✓ |
| GROQ_API_KEY | ✓ | ✓ | ✓ | ✓ | — |
| CEREBRAS_API_KEY | ✓ | ✓ | ✓ | ✓ | — |
| OPENROUTER_API_KEY | ✓ | ✓ | ✓ | ✓ | — |
| GEMINI_API_KEY | ✓ | ✓ | ✓ | ✓ | — |
| GITHUB_TOKEN_POOL | ✓ | ✓ | ✓ | ✓ | — |
| DISCORD_BOT_TOKEN | ✓ | ✓ | — | ✓ | — |
| DISCORD_WEBHOOK | ✓ | ✓ | — | ✓ | — |
| SUPABASE_SECRET_KEY | ✓ | ✓ | — | — | — |
| CLOUDFLARE_API_TOKEN | ✓ | ✓ | (self) | — | — |
| CURSOR_AUTH_TOKEN | ✓ | ✓ | (self) | — | — |
