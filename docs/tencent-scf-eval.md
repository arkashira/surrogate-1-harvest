# Tencent Cloud free-tier evaluation (2026-05-02)

User asked which Tencent Cloud products are **always-free** (not 12-month
trial). From the Free Trial portal screenshot, 6 products available.

## TL;DR

| Product | Type | Free tier | Useful for us? |
|---|---|---|---|
| **Cloud function (SCF)** | Serverless | 400k GB-sec memory + 2M invocations/mo | ✅ **YES** — IP rotation proxy, lightweight LLM router |
| **Auto Scaling (AS)** | VM scaling | "Always free" (mgmt only) | ❌ NO — needs paid CVMs underneath |
| **BatchCompute** | Batch jobs | "Always free" (mgmt only) | ❌ NO — same, paid compute |
| **Tencent Kubernetes Engine (TKE)** | k8s | "Always free" (mgmt plane only) | ❌ NO — worker nodes paid |
| **TencentCloud Lighthouse** | VM trial | 2C2G 3 months (sold out today) | 🟡 MAYBE later — trial expires |
| **Cloud Virtual Machine (CVM)** | VM trial | 2C4G 3 months (sold out today) | 🟡 MAYBE later — trial expires |

**Only SCF is genuinely always-free for serverless workloads.**

## SCF — what it gives us

```
Resource usage:    400,000 GB-seconds memory / month
Call times:        2,000,000 invocations / month
From Nov 2021:     all SCF accounts get this regardless of plan
```

That's ~equivalent to AWS Lambda free tier. At average 256MB / 200ms per call:
  400,000 / (0.256 * 0.2) ≈ 7,800,000 invocations capacity from memory budget alone

Also 2M invocations cap is the binding constraint = **~67k invocations/day, ~46/min sustained**.

## Where SCF fits in our stack (potential roles)

1. **Secondary IP-rotation probe**
   Currently /probe is on Cloudflare Worker (CF egress). Tencent SCF gives us
   a different IP pool (Tencent egress). Sites that block both GCP+CF could
   still slip through Tencent.
   - Effort: small (deploy 1 SCF function + add to research-daemon as 3rd
     fallback after CF probe + archive.org).
   - Value: low-medium. CF probe already covers ~95% of bypass needs.

2. **Pain-validator LLM proxy**
   Validator's call_llm_strong currently hits 6 providers from GCP. If GCP
   IP gets rate-limited cluster-wide on a provider, we could route via SCF.
   - Effort: medium (replicate provider chain in Tencent function).
   - Value: low. Better to add more LLM providers than rotate IPs.

3. **Heartbeat aggregator (high-throughput)**
   If we ever scale past 100 daemons, current CF Worker /agent/heartbeat
   could hit CF Worker free quota (100k req/day). SCF could absorb overflow.
   - Effort: small.
   - Value: low (we're at 30 daemons, far from quota).

4. **HF dataset / Hugging Face mirror cache**
   When HF rate-limits us, SCF function could pre-cache popular datasets
   into Tencent COS (object storage) — but COS is NOT free, so no.

## Verdict — defer

SCF is a real free option, but the use cases are all marginal value adds.
The pipeline doesn't NEED SCF right now. **Defer until** one of:
  a) CF Worker /probe gets blocked at scale (e.g. 90% of probes 403)
  b) CF Worker request budget hits 80% (we'd run out of headroom)
  c) Tencent account verification completes painlessly (no Chinese ID
     required for international account)

## Verification gotcha

Tencent international accounts often require:
- Credit card OR
- Chinese phone number + Alipay/WeChat verification

If verification works on Thai phone + visa, proceed. If it asks for
Chinese ID upload, abort — friction not worth marginal feature value.

## What we DEFINITELY skip

- **Auto Scaling** alone (no underlying VM — useless)
- **BatchCompute** alone (same)
- **TKE** management plane (worker nodes paid)
- **Lighthouse / CVM trials** (3 months only, sold out, paid after)

---

Decision: **Tencent SCF noted as future option, not provisioned now.**
Documented per free-tier-only-policy.md decision rule. Will revisit when
CF Worker hits quota OR when a specific bypass case needs Tencent egress.
