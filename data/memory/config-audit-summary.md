# Config Audit — 2026-04-23 11:53

## Issues Found
- ❌ Primary = openrouter free (rate-limited 429 constantly)
- ❌ Fallback includes Gemini (daily quota exhausted) + 4x OpenRouter (single-provider risk)
- ❌ timezone empty → cron times ambiguous
- ❌ show_cost=false hides token cost from user
- ❌ streaming.enabled=false — responses only show at end (slow UX)
- ❌ logging.max_size_mb=5 — with 400+ sessions/hr, log rotates too aggressively
- ❌ delegation uses openrouter free — rate-limited during heavy delegation

## Fixes Applied
- ✅ model.default: openrouter/gpt-oss-120b:free → deepseek-chat-v3.1:free (higher quota)
- ✅ Removed Gemini from fallback (exhausted quota); diversified OpenRouter models across coder/generalist/70b/120b tiers
- ✅ timezone: '' → 'Asia/Bangkok'
- ✅ display.show_cost: false → true
- ✅ display.bell_on_complete: false → true (audio alert on long runs)
- ✅ display.show_reasoning: false → true (visibility for debug)
- ✅ streaming.enabled: false → true
- ✅ logging.max_size_mb: 5→50, backup_count: 3→10 (keep 500MB rolling logs)
- ✅ memory limits 8k/4.5k → 16k/8k (richer context, file_read_max is 200k anyway)
- ✅ delegation.model: nvidia/nemotron → qwen/qwen3-coder (better rate limit handling)
- ✅ session_reset.idle_minutes: 1440 (24h) → 4320 (72h) for long-running research
- ✅ security.website_blocklist: enabled with ad-tracker + malware blocklist
- ✅ fallback_model → aligned with qwen-coder (same as delegation)
