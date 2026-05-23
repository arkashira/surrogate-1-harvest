# GitHub Account Usage Policy

> Set 2026-05-02 by user directive. Each GitHub account has a strict role.
> Misuse of `ashirap` (codespaces) burns quota that's reserved for dataset
> work and is **forbidden**.

## Account → Allowed Use

| Account | Codespaces | AI free APIs (Models, Copilot) | Git clone (5000/h) | Notes |
|---|---|---|---|---|
| `ashirap` | ❌ FORBIDDEN | ✅ | ✅ | Reserved for free AI APIs + dataset clone work only. **Never start a codespace here.** |
| `midnightcrisis` | ❌ EXHAUSTED | ✅ | ✅ | Codespace 60h/mo quota burned this month. Re-eligible next billing reset. |
| `ashirapit` | ✅ PRIMARY | ✅ | ✅ | New token (2026-05-02), full quota. Owns the live ollama-LLM-proxy codespace. |

## Active Codespace

- Repo: `arkashira/surrogate-1-harvest`
- Name: `ollama-llm-proxy-r49955gvjxqv3ww4`
- Owner: `ashirapit`
- Machine: `basicLinux32gb` (2-core, 8 GB RAM, 32 GB disk — free tier)
- Idle timeout: 30 min
- Port 11434 (ollama) → public via `gh codespace ports visibility 11434:public`
- Public URL: `https://ollama-llm-proxy-r49955gvjxqv3ww4-11434.app.github.dev`

## Quota Math

Free tier = 60 core-hours/month/account on basicLinux32gb (= 2 cores).
- Active hour = 2 core-hours.
- 60 / 2 = **30 wall-clock hours/month** of running codespace.
- Strategy: keepalive only during working hours UTC 0–12 (Bangkok 7am–7pm),
  ping every 20 min. Outside the window the codespace auto-stops at the
  next 30 min idle window.
- Daily burn: ≤12h × 1 codespace = 12 wall-clock hours/day during business
  days. Tight on monthly cap — if we run >25 working days/month, switch
  the keepalive to alternate days OR drop window to 0–8 UTC.

## Ops Quick Reference

```bash
# Switch active gh user (LaunchAgent does this automatically)
gh auth switch --user ashirapit

# Check codespace state
gh codespace view -c ollama-llm-proxy-r49955gvjxqv3ww4 --json state,name

# Stop early to save quota
gh codespace stop -c ollama-llm-proxy-r49955gvjxqv3ww4

# Re-publish port if private after restart (rare)
gh codespace ports visibility 11434:public -c ollama-llm-proxy-r49955gvjxqv3ww4

# Recreate (only if container hopelessly broken)
gh codespace delete -c ollama-llm-proxy-r49955gvjxqv3ww4 --force
gh codespace create --repo arkashira/surrogate-1-harvest --branch main \
    --machine basicLinux32gb --display-name "ollama-llm-proxy" --idle-timeout 30m
```

## Wiring into the Daemon Chain

The pipeline auto-uses the codespace as the deepest free LLM fallback when
`CODESPACE_LLM_URL` is set in env. Both VMs need:

```env
CODESPACE_LLM_URL=https://ollama-llm-proxy-r49955gvjxqv3ww4-11434.app.github.dev
CODESPACE_LLM_MODEL=qwen2.5-coder:7b-instruct-q4_K_M
```

Set in `/etc/surrogate-coordinator.env` on each VM, then
`systemctl restart` the daemons that import `axentx_pipeline`.

## Rationale (why ashirap is locked out)

User runs ~5000 git-clone/h pulling dataset shards from arkashira/* and
huggingface mirrors using `ashirap`'s token (highest rate-limit headroom
of the three). If a codespace also runs there it competes for the same
60h/mo bucket and risks both starving. `ashirapit` is dedicated, so the
LLM proxy stays warm without endangering dataset throughput.
