# HARD CONSTRAINT: Free-tier only, no card swipes

> "ห้ามใช้นอกฟรี ... ไม่อยากเสียตัง เน้นฟรี" — ฟิวส์, 2026-05-02

This is the project's **economic primary directive**. Every infra decision
must respect it before any other criterion (speed, capacity, ergonomics).

## Allowed (always-free, no card OR card-as-anti-fraud-only)

| Service | Tier | Hard limit | Notes |
|---|---|---|---|
| **GCP Compute e2-micro** | Always Free | 1 instance, us-central1/us-east1/us-west1 | 1 vCPU burst, 1GB RAM, 30GB SSD, 1GB egress NA-NA, 1GB to other regions/mo |
| **OCI A1.Flex (ARM)** | Always Free | 4 OCPU + 24GB RAM total across all instances | Capacity-bound — must catch open windows. Watcher running. |
| **OCI E2.1.Micro (x86)** | Always Free | 2 instances | Often "Out of host capacity" too. |
| **Cloudflare Workers** | Free | 100k req/day, 10ms CPU/req | We use ~5% currently. |
| **Cloudflare D1** | Free | 100k writes/day, 5M reads/day, 5GB | heartbeat is in here now (post-KV-quota-blowout 2026-05-02). |
| **Cloudflare KV** | Free | 1k writes/day, 100k reads/day | We blew the writes quota with heartbeats — DO NOT USE for hot writes. Reads only / cold writes only. |
| **Cloudflare Vectorize** | Free | 30M queried vectors/mo, 5M stored | RAG corpus lives here. |
| **Cloudflare AI Gateway** | Free | low daily req | Use sparingly. |
| **Cloudflare Workers AI** | Free | 10k neurons/day | LLM fast-path uses this. |
| **HuggingFace Spaces (CPU Basic)** | Free | sleeps after 48h idle | Surrogate-1 v1 inference Space lives here. |
| **HuggingFace Datasets/Models** | Free | unlimited public, 5GB private | All training pairs + adapters here. |
| **HF Hub Inference API** | Free | rate-limited (271k 429s/7d when abused — fixed) | Throttle in code. |
| **Supabase** | Free | 500MB DB, 2GB egress, paused after 1 week idle | customer_polls + paused projects. |
| **GitHub Free** | Free | unlimited public repos, 2k Actions min/mo | All axentx/* repos. |
| **GitHub Actions** | Free | 2000 min/mo private | We don't use yet. |
| **Kaggle** | Free | 30hr GPU/wk + T4×2 weekly | Surrogate-1 training runs here. |
| **Lightning AI** | Free trial | $15/mo credit, 1 instance, sleeps when idle | Used for bigger training jobs. |
| **Modal** | Free trial | $30/mo credit | Burst GPU when needed. |
| **Discord** | Free | API rate limit only | All bot/webhook traffic. |

## Conditionally allowed (free trial only — STOP before trial ends)

| Service | Trial | Action on expiry |
|---|---|---|
| ~~**Kamatera**~~ | ~~$100/30 days~~ | **DO NOT USE** — risk of auto-charge after trial. Project's free-tier policy supersedes. (2026-05-02 abandoned by user directive.) |
| **AWS** | 12 months t2/t3.micro | Set billing alerts; manual review at month 11 |
| **Azure** | 12 months B1s | Same |

## Hard rejected (ever)

- **Anything paid up-front** (DigitalOcean droplets, Linode, Vultr).
- **Pay-as-you-go on a cloud where free tier is exhausted** — never auto-fall back from free → paid.
- **Residential proxy services** ($5-50/mo) — Bright Data, Oxylabs, Smartproxy.
- **Dedicated cloud GPUs** — RunPod, Vast.ai (use Kaggle/Modal trial instead).
- **Branded SaaS that requires SSO subscription** — only what GitHub Free allows.

## Decision rule for any new service consideration

Before adding ANY new dependency, the agent MUST verify:

1. Has free tier? **Yes** → check (2). **No** → reject.
2. Does free tier require a credit card? If **yes**, set billing alarm + add to monthly review checklist.
3. Does free tier auto-charge after limits exceeded? If **yes** (e.g. AWS overage), reject UNLESS we can hard-cap usage.
4. Does free tier sunset (12-month trial)? If **yes**, document expiry date + add to calendar.
5. Is the value high enough that the operator (ฟิวส์) explicitly approves? Only with explicit approval do we proceed past (3) or (4).

## Escape hatch

User can override on a per-decision basis with the literal directive:

> "ใช้ <service> ได้ ยอมเสียตัง"

Without that, default = reject + suggest free alternatives.

---

Updated 2026-05-02 after Kamatera evaluation declined. Active hard line.
