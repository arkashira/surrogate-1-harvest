# Portable deploy toolkit — axentx surrogate-1-harvest

Deploy the full pipeline (scrape → validator → spawn → business-synth → dev → commit → MVP) to **any** Linux host (Ubuntu/Debian preferred). Designed to be moved between hosts with zero manual edits.

## Why portable

Free-tier hosts churn: GCP e2-micro disk fills, Kamatera trial expires, OCI A1.Flex gets reclaimed. The pipeline must follow capacity wherever it appears. Anything host-specific is **derived from `$AXENTX_HOME` at runtime**, never baked into systemd units or scripts.

## Three scripts

| Script | When to run | What it does |
|---|---|---|
| `portable-bootstrap.sh` | Once per fresh host | Installs system deps, clones repo, sets up venv, renders systemd units, starts daemons |
| `portable-update.sh` | After each git push | Pulls new code, restarts only daemons whose code changed |
| `portable-verify.sh` | Anytime | Health check: daemon count, queue depths, cost-guard, disk/mem, last products |
| `backfill-stranded-products.sh` | One-time after 2026-05-03 fix | Pushes `/business/` for products spawned before the git-commit bug fix |

## First deploy on a new host

```bash
# 1. Get the script onto the target (one of these)
curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/portable-bootstrap.sh -o /tmp/bootstrap.sh

# OR (if you can scp from a working source)
scp -r /opt/surrogate-1-harvest user@new-host:/opt/

# 2. Run bootstrap (as root)
sudo AXENTX_REPO=https://github.com/<you>/<repo>.git \
     AXENTX_TIER=kam-2gb \
     bash /tmp/bootstrap.sh

# 3. Edit env file with secrets
sudo $EDITOR /etc/surrogate-coordinator.env

# 4. Re-run bootstrap to start daemons (idempotent)
sudo SKIP_PKG_INSTALL=1 bash /opt/surrogate-1-harvest/deploy/portable-bootstrap.sh

# 5. Verify
bash /opt/surrogate-1-harvest/deploy/portable-verify.sh
```

## Migrating between hosts (Kam → next-host)

```bash
# On the OLD host: snapshot state directories that hold queue/seen-cache
tar czf /tmp/surrogate-state.tgz \
    /opt/surrogate-1-harvest/state \
    /home/ubuntu/.surrogate/state

# Transfer to new host
scp /tmp/surrogate-state.tgz user@new-host:/tmp/

# On the NEW host:
sudo bash deploy/portable-bootstrap.sh   # or however you got the code there
sudo systemctl stop 'axentx-*'
sudo tar xzf /tmp/surrogate-state.tgz -C /
sudo chown -R ubuntu:ubuntu /opt/surrogate-1-harvest/state /home/ubuntu/.surrogate
sudo systemctl start 'axentx-*'
bash deploy/portable-verify.sh
```

The scrape dedup, pipeline queue position, and Modal cost ledger are all preserved across the move. **Kill the OLD host** before starting the NEW one — both pulling from the same Supabase queue is fine (claim_pipeline_item RPC is mutex-safe), but doubling cost-guard daemons is wasteful.

## Tier presets

`AXENTX_TIER` controls which subset of daemons starts:

| Tier | RAM | Daemons |
|---|---|---|
| `gcp-micro` | 1GB | core only (23 daemons) |
| `kam-2gb` | 2GB | core only (23 daemons) |
| `oci-arm` | 24GB | all (40+ daemons) |
| `generic` | any | all (default) |

Tier choice maps directly to memory headroom. The 23 "core" daemons cover the full ideation→ship loop; "optional" daemons (canary, perf, content, marketing, etc.) add polish. Start core-only, scale up after disk + RAM pressure stabilizes.

## Required env keys

`portable-bootstrap.sh` writes a template to `/etc/surrogate-coordinator.env` if missing. Required for any pipeline activity:

- `SUPABASE_URL`, `SUPABASE_ANON_KEY` — pipeline queue + seen cache
- `GITHUB_TOKEN` — repo creation, code commits
- `HF_TOKEN` — dataset push, archive flush
- `DISCORD_WEBHOOK` — pipeline event stream
- `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` — LLM chain (any one suffices but ≥3 = no rate-limit pauses)

Optional (more = more headroom):

- `SAMBANOVA_API_KEY`, `NVIDIA_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`
- `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` — Workers AI + AI Gateway
- `DEEPSEEK_API_KEY` — DeepSeek V3.2 + R1 (free signup credit)
- `TOGETHER_API_KEY` — Together.ai (Llama-3.3-70B-Free + Qwen2.5-72B trial)
- `GITHUB_MODELS_TOKEN` — gpt-4o-mini via Azure

## Cost-guard always starts FIRST

`portable-bootstrap.sh` enables `axentx-cost-guard-daemon` before any other unit. The user's hard rule "ฟรี only — no paid" is enforced at runtime: any Modal profile crossing 95% of the $30 free credit gets all apps killed automatically. **If cost-guard fails to start, the bootstrap exits with code 2** so the operator notices before the pipeline burns money.

## Daemon dependency order (logical, not enforced by systemd)

The pipeline self-clears if some daemons start before others — items just sit in their stage queue until a consumer picks them up. But for fastest first-run drain:

1. `cost-guard` (always first)
2. `disk-janitor`, `discord-notifier` (infrastructure)
3. `aux-orchestrator`, `scrape-orchestrator` (input feeders)
4. `reddit-stream`, `github-deep-stream` (continuous pain harvesters)
5. `pain-validator` → `market-research` → `bd` (ideation gate)
6. `product-spawner` → `business-synthesis` (instantiation)
7. `design-thinking` → `architect` → `ux` → `prd` (specification)
8. `feature-builder` → `reviewer` → `qa` → `commit` (build)
9. `mvp-validator` (verify shipped product compiles + tests)
10. `release-daemon`, `hf-flusher` (ship + archive)

`portable-bootstrap.sh` enables in this order automatically.

## Diagnostics

```bash
# All axentx units status
systemctl list-units 'axentx-*' --no-legend

# Tail one daemon
journalctl -u axentx-business-synthesis-daemon -n 100 -f

# Pipeline queue depth (uses jq + curl + Supabase)
bash /opt/surrogate-1-harvest/deploy/portable-verify.sh

# Cost guard last cycle
cat /opt/surrogate-1-harvest/state/cost-guard.state.json | jq .
```

## Why no Docker / k8s

Single-host deployment is the constraint (free tier = no orchestrator). systemd units give us:

- Auto-restart on crash (Restart=always + RestartSec=30/60)
- Memory caps per daemon (MemoryMax=128M..512M)
- Boot integration (no babysit script needed)
- Per-unit logs (`journalctl -u`)

For multi-host expansion (later), the same `.service` files work under k3s with minimal massaging — but right now the system fits comfortably on a 2GB host.
