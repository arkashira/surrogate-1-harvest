---
title: Hermes
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: true
license: mit
short_description: Surrogate-1 — autonomous AI dev orchestration
---

# Hermes — Surrogate-1 Autonomous AI Platform

Multi-agent orchestration: 161 cron jobs, scrape pipeline, RAG, Discord bot, local LLM (Gemma 4 E4B).

**Endpoint** `/` returns JSON status (ledger size, episodes, daemons running).

## Required Secrets

Set in **Settings → Variables and secrets**:

| Secret | Required | Why |
|---|---|---|
| `OPENROUTER_API_KEY` | recommended | Cloud LLM ladder |
| `GEMINI_API_KEY` | optional | Top-of-ladder fallback |
| `GITHUB_TOKEN_POOL` | recommended | Comma-separated PATs for scrape + GH Models tier |
| `DISCORD_BOT_TOKEN` | optional | Enables Discord bot |
| `DISCORD_WEBHOOK` | optional | Notifications |

## Storage

Persistent at `/data` (50 GB). Stores ChromaDB, scrape ledger, training pairs.

## License

MIT — Surrogate-1 by Ashira / axentx
