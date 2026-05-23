---
title: "Surrogate-1 SRE/SLA Capability Research"
date: 2026-04-29
session: surrogate1-honest-audit
purpose: Teach Surrogate-1 (Qwen2.5-Coder-7B + LoRA) to be a SOTA SRE/Platform Engineer
scope: Foundations -> SLO/SLI -> observability -> auto-remediation -> postmortem -> deploy automation -> training data
tags: [sre, slo, sli, sla, observability, aiops, llm, surrogate1]
---

# Surrogate-1 SRE / SLA Research (2025-2026)

> Goal: Replace human SRE on-call rotation. 24/7 cloud monitoring, SLI/SLO definition, error-budget governance, runbook execution, postmortem authoring, and progressive deploy.

## Table of Contents

1. SRE Foundations (Golden Signals, RED, USE)
2. SLI / SLO / SLA Formalization
3. Error Budget + Burn-Rate Alerting
4. Observability Stack
5. Auto-Remediation + Runbooks
6. Incident Response + Postmortem
7. Capacity Planning + FinOps
8. AIOps + LLM-Driven SRE (2025-2026)
9. Deploy Automation + Progressive Delivery
10. Cloud-Specific SRE (AWS/GCP/Azure)
11. Training Data Sources for SRE
12. Eval Strategy + Benchmarks
13. v2 Plan: How Surrogate-1 Learns SRE

---

## 1. SRE Foundations

### 1.1 The Four Golden Signals (Google SRE)

> Source: Google SRE Book, "Monitoring Distributed Systems"

If you can only measure four metrics on a user-facing service, measure these:

| Signal | What it answers | Common metric |
|--------|-----------------|---------------|
| **Latency** | How long requests take | `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))` |
| **Traffic** | How much demand | `sum(rate(http_requests_total[1m]))` |
| **Errors** | How often requests fail | `sum(rate(http_requests_total{code=~"5..|429"}[5m])) / sum(rate(http_requests_total[5m]))` |
| **Saturation** | How "full" the service is | CPU >85%, queue depth, memory pressure, disk %used |

**Golden rule**: success-fail latency must be tracked separately. Failed requests often complete fast (e.g., 500 from cache miss), polluting good-latency aggregates.

### 1.2 RED Method (Tom Wilkie - Grafana)

For request-driven services / microservices:
- **R** ate: requests per second (`rate(http_requests_total[1m])`)
- **E** rrors: failed requests per second (`rate(http_requests_total{status=~"5.."}[1m])`)
- **D** uration: latency distribution (`histogram_quantile(0.95, ...)`)

**Use when**: HTTP/gRPC services, anything in front of users.

### 1.3 USE Method (Brendan Gregg)

For every **resource** (CPU, RAM, disk, NIC, queue):
- **U** tilization: % busy time
- **S** aturation: queue length / wait time (extra work that resource cannot service yet)
- **E** rrors: error events

**Use when**: hardware/kernel investigation, infra capacity, "why is the box slow" diagnosis.

### 1.4 When to use which

```
Customer-facing API down or slow?
  -> RED (Rate/Errors/Duration)   -> diagnose service behaviour

Infra resource hot (CPU/disk/net)?
  -> USE (Utilization/Saturation/Errors)   -> diagnose hardware/kernel

Quick check on a black-box service?
  -> Golden Signals (latency, traffic, errors, saturation)
```

### 1.5 Toil reduction

Toil = manual, repetitive, automatable, tactical work that grows with service size (Google SRE definition). Target: <50% toil; the rest must be engineering. 2026 industry data (incident.io State of Incident Management 2026): toil rose 30 percent year-over-year despite AIOps adoption -- automation moved up the stack but humans now do harder work that the bots can't.

Practical anti-toil moves:
- Self-service dashboards instead of "ping SRE for graph"
- Auto-scale (HPA + Karpenter) instead of manual capacity tickets
- ChatOps runbooks (`/restart-pod foo` in Slack) instead of kubectl-by-hand
- Postmortem action items must produce code/automation, not "we'll be more careful"

---

## 2. SLI / SLO / SLA Formalization

### 2.1 Definitions (precise)

| Term | Audience | Owner | Example |
|------|----------|-------|---------|
| **SLI** | Internal | SRE | "Fraction of HTTP requests with code 2xx/3xx in last 5 min" |
| **SLO** | Internal team commitment | Product + SRE | "99.9% successful requests over 28 days" |
| **SLA** | External (customer contract) | Legal + Product | "99.5% uptime/month or get 10% credit" |

Rule: **SLA must be looser than SLO**. SLO = internal early-warning; SLA = legal floor.

### 2.2 Defining a good SLI

A good SLI is:
1. **User-perceivable**: track *what users notice*, not what's easy to measure (server-side success vs end-to-end success differ).
2. **Proportional**: ratio of good events / valid events. Easier to reason about than absolutes.
3. **Bounded** with a clear validity window.
4. **Tied to a critical user journey** (CUJ): search-cart-checkout-pay.

#### Common SLI templates (event-based)

```yaml
# Availability SLI
SLI = good_events / valid_events
good = http_requests_total{code!~"5..|429", path!~"/health.*"}
valid = http_requests_total{path!~"/health.*"}

# Latency SLI (% of requests under threshold)
SLI = http_request_duration_seconds_bucket{le="0.3", path="/api/search"} / http_request_duration_seconds_count{path="/api/search"}

# Quality SLI (e.g. transcoding)
SLI = encoded_with_target_quality / total_encoded

# Freshness SLI (data pipeline)
SLI = data_age_seconds{quantile="0.95"} < 60
```

### 2.3 SLO target setting -- practical guide

| Target | Allowed downtime / 30d | Allowed downtime / quarter | Cost | When to use |
|--------|-------------------------|----------------------------|------|-------------|
| 99% | 7h 18m | 21h 54m | Low | Internal tools, beta features |
| 99.5% | 3h 39m | 10h 57m | Low-Med | Non-critical APIs |
| 99.9% (3 nines) | 43m 12s | 2h 9m | Med | **Default for production APIs** |
| 99.95% | 21m 36s | 1h 4m | Med-High | Customer-facing transactional |
| 99.99% (4 nines) | 4m 19s | 12m 58s | High | Payments, auth, identity |
| 99.999% (5 nines) | 25.9s | 1m 17s | Very high | Telco core, SS7, Cloud regional control plane |

**Setting heuristic**:
1. Measure your current 30d SLI (don't set fictional targets).
2. Subtract a small margin (e.g., current 99.95% -> target 99.9%) to leave error budget.
3. If your current state is < 99%, set the SLO at 99% and run a reliability project.
4. Tighter SLO = exponentially more engineering cost. Each additional "nine" roughly 10x the spend.

### 2.4 Time windows

| Window | Use | Pros | Cons |
|--------|-----|------|------|
| **Rolling 28-day** | Most common | Stable, smooths weekend traffic, fits 4 weeks | Can hide a recent regression behind old budget |
| **Rolling 7-day** | Burn-rate fast loop | Sensitive | Noisy, weekly seasonality |
| **Calendar quarter** | Exec reporting | Aligns to OKRs | Resets, encourages YOLO at quarter-end |
| **Calendar month** | Compliance | Matches SLA contract | Same reset issue |

**Best practice**: report rolling 28-day to engineers, calendar month to customers (SLA), calendar quarter to execs.

### 2.5 SLO tooling (2025-2026)

| Tool | Type | Strength | Weakness |
|------|------|----------|----------|
| **OpenSLO** (spec only) | YAML spec | Vendor-neutral standard | No engine, just schema |
| **Sloth** | Generator (CLI/operator) | Auto-generates Prometheus recording + multi-window multi-burn alerts | No UI |
| **Pyrra** | Generator + UI | Built-in dashboard, Thanos-friendly subquery aggregation | Heavier than Sloth |
| **Nobl9** | SaaS platform | Composite SLOs, anomaly detection, replay, SLI from Datadog/New Relic/CW | Paid |
| **Grafana SLO** | Grafana plugin | UI in same place as dashboards | Tied to Grafana stack |
| **Datadog SLO** | SaaS | Native to DD telemetry | DD lock-in |

CNCF GitOps Survey 2025: 91% cloud-native shops adopted GitOps, and Sloth+Pyrra are the dominant open-source SLO stack.

#### Sloth YAML example (real, generates Prom rules + alerts)

```yaml
version: "prometheus/v1"
service: "checkout-api"
labels:
  owner: "payments-team"
  repo: "myorg/checkout-api"
  tier: "1"
slos:
  - name: "requests-availability"
    objective: 99.9
    description: "99.9% of HTTP responses are non-5xx and non-429"
    sli:
      events:
        error_query: |
          sum(rate(http_request_duration_seconds_count{job="checkout-api",code=~"(5..|429)"}[{{.window}}]))
        total_query: |
          sum(rate(http_request_duration_seconds_count{job="checkout-api"}[{{.window}}]))
    alerting:
      name: CheckoutHighErrorRate
      page_alert:
        labels: { severity: page, routing_key: payments-pd }
      ticket_alert:
        labels: { severity: ticket, slack_channel: "#alerts-payments" }

  - name: "requests-latency-p99"
    objective: 99.5
    description: "99.5% of /checkout requests complete in <300ms"
    sli:
      events:
        error_query: |
          sum(rate(http_request_duration_seconds_count{job="checkout-api",path="/checkout"}[{{.window}}]))
          -
          sum(rate(http_request_duration_seconds_bucket{job="checkout-api",path="/checkout",le="0.3"}[{{.window}}]))
        total_query: |
          sum(rate(http_request_duration_seconds_count{job="checkout-api",path="/checkout"}[{{.window}}]))
    alerting:
      name: CheckoutSlowLatency
      page_alert:
        labels: { severity: page }
```

Run `sloth generate -i slo.yaml -o prom-rules.yaml` -> produces ~30 lines of Prom recording rules + multi-window multi-burn alerts.

---

## 3. Error Budget + Burn-Rate Alerting

### 3.1 Error budget math

```
SLO target = 99.9%
Allowed unreliability = 100% - 99.9% = 0.1%
Window = 30 days = 43,200 minutes
Error budget = 0.1% * 43,200 min = 43.2 min of "bad" allowed
```

### 3.2 Burn rate

Burn rate = rate of consuming error budget vs the steady-state rate.
- Burn rate of 1.0 = consume budget evenly over the window (= you'll exhaust at month end).
- Burn rate of 14.4 = consume entire 30-day budget in ~50 hours.
- Burn rate of 36 = consume entire budget in <24 hours.

Formula:
```
time_to_exhaust = (1 - SLO) / (error_ratio * window_size * burn_rate)
```

### 3.3 Multi-window multi-burn-rate alerts (Google SRE Workbook)

For 99.9% SLO, the canonical config is:

| Severity | Long window | Short window | Burn rate factor | Threshold (error ratio) | % budget burned |
|----------|-------------|--------------|------------------|-------------------------|-----------------|
| **Page** | 1h | 5m | 14.4 | 14.4 * 0.001 = 1.44% errors | 2% in 1h |
| **Page** | 6h | 30m | 6 | 6 * 0.001 = 0.6% errors | 5% in 6h |
| **Ticket** | 24h | 2h | 3 | 3 * 0.001 = 0.3% errors | 10% in 1d |
| **Ticket** | 3d | 6h | 1 | 0.1% errors | 10% in 3d |

The **short window** (5m/30m/2h/6h) is required to prevent the alert from staying lit after the burn stops.

#### Prometheus alert rule (page severity)

```yaml
- alert: SLOBurnRate_Page_Fast
  expr: |
    (
      job:slo_errors_per_request:ratio_rate1h{job="checkout-api"}  > (14.4 * 0.001)
      and
      job:slo_errors_per_request:ratio_rate5m{job="checkout-api"}  > (14.4 * 0.001)
    )
    or
    (
      job:slo_errors_per_request:ratio_rate6h{job="checkout-api"}  > (6 * 0.001)
      and
      job:slo_errors_per_request:ratio_rate30m{job="checkout-api"} > (6 * 0.001)
    )
  for: 2m
  labels:
    severity: page
    slo: "99.9_availability"
  annotations:
    summary: "Checkout SLO fast-burn ({{ $value | humanizePercentage }})"
    runbook_url: "https://runbooks.example.com/checkout-burn-fast"
    dashboard_url: "https://grafana.example.com/d/abc/checkout-overview"
```

Recording rules (precomputed -- one per window):

```yaml
- record: job:slo_errors_per_request:ratio_rate5m
  expr: |
    sum by (job) (rate(http_requests_total{code=~"5..|429"}[5m]))
    /
    sum by (job) (rate(http_requests_total[5m]))

- record: job:slo_errors_per_request:ratio_rate1h
  expr: |
    sum by (job) (rate(http_requests_total{code=~"5..|429"}[1h]))
    /
    sum by (job) (rate(http_requests_total[1h]))
# ... repeat for 5m, 30m, 1h, 2h, 6h, 1d, 3d
```

### 3.4 Error-budget policy (governance)

A real org needs a *written* policy. Template:

```markdown
# Error Budget Policy: checkout-api

## SLO
- 99.9% availability over rolling 28d.

## When error budget is HEALTHY (>= 25% remaining)
- Ship features at normal cadence.
- Optional: roll out to 100% canary.

## When error budget is BURNING (5-25% remaining)
- Heightened review on infra changes.
- Mandatory canary at 1% -> 10% -> 50% -> 100% with 10m soak each.
- Stand up a reliability ticket for the top contributor.

## When error budget is EXHAUSTED (< 5% remaining)
- FREEZE feature deploys (only fixes that improve reliability).
- Daily reliability standup.
- Quarterly retrospective: do we lower the target, or invest in reliability?

## When EXHAUSTED for two consecutive windows
- Escalate to Director. Either invest engineering OR officially lower SLO with customer comms.
```

This is the "feature velocity vs reliability" trade-off baked into a policy.

### 3.5 Slow vs fast burn

- **Fast burn** = 1h / 5m windows. Means a real incident *right now*. Page someone.
- **Slow burn** = 1d / 3d windows. Means a regression that won't tip you over until next week. Ticket; fix in business hours.

Both are needed: fast catches today's outage, slow catches the new latency tail from a deploy.

---

## 4. Observability Stack

### 4.1 The three pillars (and their convergence)

| Pillar | Cardinality | Cost | Best at |
|--------|-------------|------|---------|
| **Metrics** | Low (must be) | Low | Dashboards, alerting |
| **Logs** | High | High | Forensic, debugging |
| **Traces** | Per-request | Med | Cross-service latency, dependencies |

OpenTelemetry 2025 GA brought logs alongside metrics+traces in OTLP -- a single wire format and resource model. April 2026: OBI (OpenTelemetry eBPF Instrumentation, ex-Beyla) reached v0.8.0 -- zero-code RED metrics + spans for any HTTP/gRPC service on Linux. CNCF Q1 2026: 67% of prod K8s clusters run an eBPF-based observability tool.

### 4.2 Stack choices (open-source)

```
Metrics:    Prometheus (long-term: Thanos / Mimir / VictoriaMetrics)
Logs:       Loki  (cost) or  Elasticsearch / OpenSearch (full-text)
Traces:     Tempo / Jaeger v2 (now built on OTel Collector)
Collection: OpenTelemetry Collector (receives OTLP, routes everywhere)
Dashboards: Grafana
Alerts:     Alertmanager
Auto-instr: OBI (eBPF) for RED/spans without code change
```

Loki vs Elasticsearch (2026 data):
- Loki only indexes labels. 1 TB/day raw -> 50-100 GB stored after compression (S3) = a few $/month.
- Elasticsearch full-text indexes -> multi-copy data, often 3-5x raw size, plus ~8-16 GB RAM hot-tier.
- Choose Loki for K8s + cost; Elasticsearch for full-text security/audit search.

### 4.3 Commercial APM comparison

| Tool | Strength | Weakness | 2026 status |
|------|----------|----------|-------------|
| **Datadog** | Best all-in-one, Bits AI SRE | $$$ at scale (custom metrics blow up bill) | Industry default |
| **New Relic** | NRQL, generous free tier | Smaller community than DD | Acquired Pixie (eBPF) |
| **Honeycomb** | High-cardinality events, BubbleUp ML | Smaller ecosystem | Charity Majors / ODD champion |
| **Dynatrace** | Auto-discovery, Davis AI | Closed, expensive | Strong in legacy enterprise |
| **Splunk** | Logs + security (SIEM) | Painful pricing | Cisco-owned (2024) |
| **Grafana Cloud** | OSS-compatible managed | Multi-product; some are nascent | Donated Beyla -> OBI |

### 4.4 eBPF-based observability

eBPF lets you instrument the kernel without sidecars or code changes.
- **Pixie** (CNCF, ex-New Relic): K8s-native, captures HTTP/gRPC/DNS/Postgres protocols; auto-mTLS, auto-traces.
- **Cilium Hubble**: L3/L4 network flow + L7 (HTTP/gRPC). Exports to OTel.
- **Parca / Polar Signals**: continuous profiling.
- **OBI / Beyla**: zero-code RED metrics + spans (donated to OTel).

When to use eBPF: legacy code you can't re-instrument, polyglot service mesh, perf at p99 tail.

### 4.5 Distributed tracing essentials

Standard semantic attributes (OTel):
```
service.name, service.version, service.namespace
http.method, http.route, http.status_code
db.system, db.statement (sanitized!), db.operation
messaging.system, messaging.destination
gen_ai.system, gen_ai.request.model, gen_ai.usage.input_tokens   # NEW 2026 GenAI conventions
```

Sampling: head-based (decision at root span) is cheap but loses interesting tails. Tail-based (collector buffers and decides) catches slow/error traces.

### 4.6 Alertmanager routing tree

```yaml
route:
  group_by: ['alertname', 'cluster', 'service']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: 'default-slack'
  routes:
    - match: { severity: page }
      receiver: pagerduty
      continue: true
    - match: { severity: page, team: payments }
      receiver: pagerduty-payments
    - match_re: { service: '^(checkout|cart)$' }
      receiver: slack-payments

inhibit_rules:
  # If a node is down, suppress per-pod alerts on that node
  - source_match: { alertname: NodeDown }
    target_match_re: { alertname: 'Pod.*' }
    equal: ['node']

receivers:
  - name: pagerduty
    pagerduty_configs:
      - service_key: ${PD_KEY}
        severity: '{{ if eq .CommonLabels.severity "page" }}critical{{ else }}warning{{ end }}'
  - name: slack-payments
    slack_configs:
      - channel: '#alerts-payments'
        title: '{{ .CommonAnnotations.summary }}'
        text: '<{{ .CommonAnnotations.runbook_url }}|Runbook>'
```

Three Alertmanager primitives Surrogate-1 must master:
1. **Routing tree** -- match label -> receiver.
2. **Grouping** -- collapse N alerts of the same kind into one notification.
3. **Inhibit/Silence** -- root-cause alert suppresses symptom alerts; silences mute during maintenance.

---

## 5. Auto-Remediation + Runbooks

### 5.1 Common runbook patterns

| Symptom | Detection | Action | Risk |
|---------|-----------|--------|------|
| Pod CrashLoopBackOff | k8s event | `kubectl rollout restart` or `kubectl delete pod` | Low |
| OOMKilled (exit 137) | container terminated | Increase memory limit; re-deploy | Med |
| ImagePullBackOff | k8s event | Validate registry creds, image tag | Low |
| PVC Pending | k8s event | Check StorageClass, PV availability, region | Med |
| HPA can't scale up | HPA event "FailedGetResourceMetric" | Check metrics-server / cluster autoscaler / Karpenter | Med |
| Region degraded | DNS health check / SLO burn in single region | Failover Route 53 / Global Accelerator | High (data loss risk) |
| Disk full | USE saturation = 100% | Rotate logs, expand EBS, GC | Med |
| Cert expiring | cert-manager alert / x509 SAN check | Renew (cert-manager auto) | Low |
| Bad deploy | SLO burn within 10m of deploy | `argo rollouts abort` or `kubectl rollout undo` | Low (if done <30m) |
| Hot DB | RDS CPU >90, slow queries | Read-replica failover; kill long-running query | High |

### 5.2 Runbook template (markdown)

```markdown
# Runbook: <ServiceName> High Error Rate

## Trigger
Alert: `<ServiceName>HighErrorRate`
Severity: page
Burn-rate: 14.4x for 1h / 5m

## TL;DR
Most likely cause: bad deploy in last 30m. Roll back first; investigate after.

## Quick check (60 sec)
1. `kubectl rollout history deployment/<svc>` -- any deploy in last 1h?
2. Check Grafana dashboard: <link>
3. Recent commits: <link to GH compare>

## Mitigation steps (in order)
### Step 1: Rollback if recent deploy
`kubectl rollout undo deployment/<svc>`
Verify SLI returns to normal within 5 min. If yes, stop here.

### Step 2: Scale up if saturation
`kubectl scale deployment/<svc> --replicas=<2x current>`
Pre-check HPA upper bound; raise if needed.

### Step 3: Failover if region issue
Update Route 53 weighted record: <region-A>=0, <region-B>=100.

## Post-mitigation
- Page Incident Commander
- Open postmortem in Notion / Slack incident channel
- Tag commit / runbook executed in incident timeline

## Related
- Dashboard: <url>
- SLO doc: <url>
- Owner: @payments-team
```

### 5.3 Auto-remediation tools

| Tool | Style | Strength |
|------|-------|----------|
| **Rundeck** (PD-owned) | Job runner, web UI + API | Self-service, RBAC, audit log; K8s plugin |
| **StackStorm** | Event-driven (IFTTT for ops) | Sensors + rules + actions; mature |
| **Shoreline** | Ops console + auto-fix DSL | K8s-native, reduces toil |
| **Ansible AWX / Tower** | Playbooks | Standard in many shops |
| **AWS Systems Manager (SSM)** | Native AWS | Run Command, Automation runbooks (YAML) |
| **PagerDuty Runbook Automation** | (Rundeck OEM) | Tight PD integration |
| **Argo Workflows** | K8s-native DAG | Good for multi-step remediation |

### 5.4 Self-healing K8s primitives

K8s already gives you:
- **Liveness probe** -> restart pod
- **Readiness probe** -> remove from service endpoints
- **Startup probe** -> guard slow boots
- **Pod Disruption Budget** -> protect during eviction
- **HPA / VPA / KEDA / Karpenter** -> autoscale
- **Operators** (CRD + controller pattern) -> domain-specific self-healing (e.g., postgres-operator handles failover)

### 5.5 LLM-driven runbook execution (2026)

Pattern emerging in 2026 (incident.io, Datadog Bits AI SRE, Rootly AI):
1. Alert fires.
2. LLM agent is given alert payload + service catalog + recent deploys + observed signals.
3. Agent runs diagnostic *queries* (PromQL, kubectl, log search) via tools.
4. Agent emits a hypothesis with citations.
5. Agent **proposes** a runbook step. Human approves -> agent executes -> agent verifies SLI back to normal.

Critical safety boundary:
- Read-only diagnosis: fully autonomous OK.
- Mutating actions (rollback, scale, restart): human approval gate (required by every vendor).
- Destructive actions (DB DROP, region failover, force re-keying secrets): always human.

---

## 6. Incident Response + Postmortem

### 6.1 Incident command (PagerDuty model)

Roles:
- **Incident Commander (IC)** -- owns the bridge, makes calls, NOT debugging.
- **Scribe** -- timeline-keeping, status updates.
- **Subject Matter Experts (SMEs)** -- per-service domain specialists.
- **Communications Liaison** -- updates status page, customer comms.

Severity ladder (typical):
- **SEV-1**: customer-impacting outage; CEO/CTO notified; war room; SLA at risk.
- **SEV-2**: major degradation; on-call team only; SLO at risk.
- **SEV-3**: minor or partial; ticket-driven.
- **SEV-4**: cosmetic / single-customer.

### 6.2 Postmortem template (PagerDuty + Google blend)

```markdown
# Postmortem: <Service> outage <YYYY-MM-DD>

## Status
- Severity: SEV-1
- Date: 2026-04-29
- Duration: 47 minutes
- IC: @alice
- Scribe: @bob

## Summary
One-paragraph plain-English description -- contributing factors + impact.

## Impact
- Customer requests failed: 1.2M (12% of normal)
- Time in SEV-1: 47 min
- SLO budget consumed: 67% of monthly checkout budget
- Tickets generated: 38
- Revenue impact (est): $42K

## Timeline (UTC, with evidence links)
- 14:02 -- @charlie deploys checkout-api v3.4.0 (commit abc123)
- 14:09 -- Sloth burn-rate alert fires (Slack)
- 14:11 -- @alice declared IC (PD)
- 14:14 -- Logs show NullPointerException in PaymentService.charge() (Honeycomb link)
- 14:18 -- @bob runs `kubectl rollout undo deployment/checkout-api`
- 14:22 -- Error rate returns to baseline (Grafana link)
- 14:49 -- Incident closed

## Root Cause / Contributing Factors
> Note: complex systems do not have a single root cause. List contributing factors.

1. Deploy v3.4.0 introduced a code path that called `paymentMethod.id` without null guard.
2. Unit tests passed because mocks always provided non-null `paymentMethod`.
3. Canary stage was 5%; the bug only manifested under guest-checkout flow which is 8% of traffic, so canary missed it.
4. The pre-prod env doesn't include guest-checkout traffic shadowing.

## Detection
- How: SLO burn-rate page (1h/5m, 14.4 burn).
- Time-to-detect (TTD): 7 minutes after deploy.
- Could we have detected sooner? Yes -- a 5-minute fast burn would have paged in 2 min.

## Resolution
- Short-term: rolled back v3.4.0 -> v3.3.4.
- Long-term: see Action Items.

## What went well
- Rollback worked first try.
- Communication on Slack was clean; status page updated within 6 min.

## What went poorly
- 5-minute burn alert wasn't configured for checkout (only 1-hour was).
- Pre-prod doesn't shadow guest-checkout traffic.
- The PR review missed the null-guard.

## Action Items
| ID | Action | Owner | Priority | Status |
|----|--------|-------|----------|--------|
| AI-1 | Add 5m fast-burn alert for checkout-api | @alice | P0 | Open |
| AI-2 | Add unit test asserting null `paymentMethod` is rejected | @charlie | P0 | Open |
| AI-3 | Pre-prod traffic-shadowing for guest-checkout | @platform | P1 | Open |
| AI-4 | Lint rule: `paymentMethod\.id` without preceding null check fails CI | @dev-ex | P2 | Open |

## Lessons learned
- Canary % must be larger than the smallest user segment we care about.
- Schema-level null assertions catch bugs that mocks hide.

## 5 Whys
1. Why did checkout fail? -> NullPointerException in charge().
2. Why was paymentMethod null? -> Guest checkouts return a session-scoped payment with no DB id.
3. Why didn't tests catch it? -> Mocks always set id.
4. Why didn't canary catch it? -> Guest is 8% of traffic, canary was 5%.
5. Why was canary set to 5%? -> No data-driven sizing rule; default copied from another service.
```

### 6.3 Blameless principle

Hard rules:
- Never use a person's name + an action verb in the same sentence as a defect ("Charlie's PR broke prod" -> NO).
- Always ask "what conditions made this *possible*", not "who did this".
- Action items must be technical/process, never "be more careful".

### 6.4 5-Whys + Fishbone

5-Whys is *one* technique; in complex systems use it as a starting branch and pair with Fishbone (Ishikawa) categories: People, Process, Tooling, Code, Configuration, External.

### 6.5 Action item tracking

Postmortem ROI is in *executed* action items. Default: any AI not closed in 30 days is escalated; >60 days = exec review. Tag commits with the postmortem id (`POSTMORTEM-2026-04-29`) so the corpus has retrievable AI->commit links.

---

## 7. Capacity Planning + FinOps

### 7.1 K8s scaling layers (the Big Five)

| Layer | Tool | Scales | Speed |
|-------|------|--------|-------|
| Pod replicas | **HPA** | replica count by CPU/mem/custom metric | 15-60s |
| Pod size | **VPA** | requests/limits | minutes (recreates pod) |
| Custom triggers | **KEDA** | replicas by Kafka lag, SQS depth, cron, etc. | 15-30s |
| Node pool | **Cluster Autoscaler** | adds nodes from ASG | 3-5 min |
| Just-in-time nodes | **Karpenter** | provisions optimal-fit instance directly | <30s |

Rules of thumb:
- HPA + Karpenter: default for 2026 on AWS EKS.
- VPA in *recommendation only* mode if HPA is on the same metric (don't let them fight).
- KEDA for queue-driven work.

### 7.2 FinOps -- right-sizing

CNCF data (2026): cloud waste ~30% even after 4 years of FinOps. Why: dev sets memory `requests = limits = 2Gi` for safety; pods use 200Mi.

Workflow (FinOps Foundation):
1. **Inform**: Vantage / CloudHealth / Cast.ai dashboards. Show team-level spend.
2. **Optimize**: VPA recommendations, Karpenter consolidation, switch to ARM (Graviton -- ~20% cheaper), spot for stateless.
3. **Operate**: budgets + anomaly detection + chargeback.

### 7.3 Spot / preemptible / RI / Savings Plan mix (AWS)

| Workload | Best buy |
|----------|----------|
| Stateless, batch, retryable | **Spot** (60-90% off) |
| Stateful, can tolerate eviction with retry | Spot with PDB + checkpoint |
| Always-on baseline (24/7) | **Compute Savings Plan** 1y or 3y (~40-72% off) |
| Memory-heavy (RDS, ElastiCache) | **Reserved Instance** (DB SP doesn't exist for all DB types yet) |
| Bursty | On-demand + Karpenter |

### 7.4 Tooling 2026

- **Vantage** -- multi-cloud + FinOps + 20+ integrations (incl. Datadog, Snowflake, OpenAI).
- **Cast.ai** -- K8s-only, automated bin-packing, GPU-aware (good for ML).
- **Spot.io (NetApp/Flexera)** -- spot-instance automation.
- **Cloudability / Apptio** -- enterprise FinOps.
- **AWS native**: Compute Optimizer, Cost Explorer, Trusted Advisor, S3 Intelligent-Tiering.

### 7.5 GPU capacity for ML / LLM workloads

Special concerns Surrogate-1 must learn:
- GPU spot on AWS (g5/g6) ~70% discount but evicts in seconds; use checkpointing.
- A100/H100 supply-constrained -- reserve via Capacity Blocks or use Lightning.ai / Modal.
- KV-cache memory dominates inference cost; right-size context length.
- Cast.ai + Karpenter can schedule GPU pods on cheapest available SKU.

---

## 8. AIOps + LLM-Driven SRE (2025-2026)

### 8.1 Generations

- **AIOps 1.0** (2018-2022): noise reduction, alert dedup, statistical correlation. Moogsoft, BigPanda.
- **AIOps 2.0 / AI SRE** (2024-2026): generative agents, multi-step reasoning, RAG over runbooks/postmortems, code-aware root-cause.

### 8.2 Key vendors / projects

| Tool | Approach | What it does |
|------|----------|--------------|
| **Datadog Bits AI SRE** | LLM grounded in DD telemetry + 1000s of past incidents; claims 90% faster RCA | Auto-investigate, suggest fix; Dev Agent opens PR |
| **incident.io AI SRE** | Multi-agent investigation; Slack-native | Triage, hypothesize, find PR, draft postmortem in 1-2 min |
| **Rootly AI** | AI-powered IM platform | Incident summary, action items, retro |
| **Robusta** | OSS K8s monitoring + auto-remediation | Open-source AIOps for K8s |
| **Microsoft Triangle** | Multi-LLM-agent in Azure | Incident triage; 97% accuracy, -91% TTE; in prod 2024+ |
| **PagerDuty AIOps** | Event correlation + auto-runbooks | Mature, broad coverage |
| **NewRelic AI** | Errors Inbox + AI-suggested fixes | NRQL-aware |
| **Honeycomb Query Assistant** | LLM that writes Honeycomb queries | High-cardinality friendly |

### 8.3 Architecture pattern (the "AI SRE stack")

```
                        +------------------------+
   alert / page  ----> | Investigator Agent      | <-- service catalog
                       |  - read-only tools      | <-- recent deploys (GH)
                       |  - PromQL/kubectl/log   | <-- past postmortems (RAG)
                       |  - traces                |
                       +-----------+--------------+
                                   |
                          hypothesis + citations
                                   |
                                   v
                       +------------------------+
                       | Action Proposer Agent   |
                       |  - runbook retrieval   |
                       |  - rollback / scale    |
                       |  - approval gate       |
                       +-----------+------------+
                                   |
                              human OK
                                   |
                                   v
                       +------------------------+
                       | Executor Agent          |
                       |  (kubectl / argo / SSM)|
                       +-----------+------------+
                                   |
                                   v
                       +------------------------+
                       | Verifier Agent          |
                       |  - SLI back to baseline?
                       |  - new errors?         |
                       +-----------+------------+
                                   |
                                   v
                       +------------------------+
                       | Documenter Agent        |
                       |  drafts postmortem,    |
                       |  populates timeline    |
                       +------------------------+
```

### 8.4 Microsoft Triangle paper (2025) -- key insights

- Multi-agent collaboration with negotiation protocol.
- 97% triage accuracy, 91% TTE reduction in Azure.
- 6 teams in prod, 15+ onboarding.
- "Semantic distillation" -- LLM extracts action-relevant info from noisy telemetry first.

### 8.5 AIOpsLab (MLSys 2025) and ITBench (ICML 2025)

Two academic eval frameworks Surrogate-1 v2 should target:
- **AIOpsLab**: holistic eval of AI agents on autonomous cloud ops -- detection, localization, mitigation.
- **ITBench**: diverse real-world IT automation tasks (incident, K8s ops, cloud config).
- **STRATUS** (NeurIPS 2025): multi-agent system for autonomous cloud reliability.

### 8.6 Hallucination + safety

The **"AI Oops"** paper (RSA 2025, arXiv 2508.06394) showed adversaries can manipulate telemetry to mislead LLM-based AIOps -- crafted log lines steered the LLM to "fix" the wrong thing. Implication for Surrogate-1: never trust the *content* of a single log/metric source; require corroboration (logs + metrics + traces agree). Always emit a confidence score and require human approval for mutations.

### 8.7 Glass-box requirement

incident.io criterion: every AI suggestion must cite specific log lines / commits / past incidents. Surrogate-1 v2 must default to citation-grounded outputs (RAG with source tagging). No citation -> escalate to human, do not act.

---

## 9. Deploy Automation + Progressive Delivery

### 9.1 Strategies

| Strategy | Risk | Rollback time | Complexity |
|----------|------|--------------|------------|
| Recreate (kill all, redeploy) | High | minutes | trivial |
| Rolling | Med | minutes | low |
| **Blue-Green** | Low | seconds (DNS/LB swap) | med (2x infra) |
| **Canary** (1% -> 10% -> 50% -> 100%) | Low | seconds | med-high |
| **Feature flag** (ship dark, enable later) | Lowest | instant | high (flag debt) |
| **Shadow / Mirror** | Lowest (no user impact) | n/a | high (data divergence) |

### 9.2 Tooling matrix

| Tool | Layer | Strength |
|------|-------|----------|
| **Argo Rollouts** | K8s CRD replacing Deployment | Canary + analysis (Prom queries), blue-green, experimentation |
| **Flagger** | K8s + service mesh (Linkerd/Istio/NGINX) | Auto canary based on metrics; mesh-aware |
| **ArgoCD** | GitOps deploy | Pull-based; multi-cluster; UI |
| **FluxCD** | GitOps deploy | Lighter; CRD-only; deep Kustomize |
| **Spinnaker** | Multi-cloud CD | Heavy but proven (Netflix origin) |
| **LaunchDarkly** | Feature flags SaaS | Best-in-class; experimentation |
| **GrowthBook** | OSS feature flags | Cheaper; statistical experiments |
| **Unleash** | OSS feature flags | Self-hostable |

### 9.3 Argo Rollouts canary YAML (real)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: checkout-api
spec:
  replicas: 10
  strategy:
    canary:
      canaryService: checkout-canary
      stableService: checkout-stable
      trafficRouting:
        istio:
          virtualService:
            name: checkout-vs
            routes: ["primary"]
      steps:
        - setWeight: 5
        - pause: { duration: 5m }
        - analysis:
            templates:
              - templateName: success-rate-99
            args:
              - name: service-name
                value: checkout-canary
        - setWeight: 25
        - pause: { duration: 10m }
        - setWeight: 50
        - pause: { duration: 10m }
        - setWeight: 100
  revisionHistoryLimit: 5
  selector:
    matchLabels: { app: checkout }
  template:
    metadata:
      labels: { app: checkout }
    spec:
      containers:
        - name: api
          image: myorg/checkout:v3.5.0

---
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate-99
spec:
  args: [{name: service-name}]
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      failureLimit: 3
      provider:
        prometheus:
          address: http://prom:9090
          query: |
            sum(rate(http_requests_total{service="{{args.service-name}}", status!~"5.."}[2m]))
            /
            sum(rate(http_requests_total{service="{{args.service-name}}"}[2m]))
```

### 9.4 GitOps flow

```
Dev pushes to main
  -> CI runs tests, builds image, tags v3.5.0
  -> CI updates env/prod/values.yaml: tag=v3.5.0  (sometimes via Renovate / image-automation)
  -> ArgoCD/Flux detects git change
  -> Reconciles cluster to desired state
  -> Argo Rollouts runs canary analysis
  -> If analysis passes -> 100%
  -> If fails -> auto-rollback
```

ArgoCD vs FluxCD (2026):
- ArgoCD: web UI, multi-tenant, RBAC, more features. Resource-heavy beyond ~3-5K apps.
- FluxCD: CRD-only, lighter, deep Kustomize/image automation, no UI by default.
- Both support Helm, Kustomize.

### 9.5 Service mesh role in delivery

| Mesh | Latency cost (mTLS) | mTLS approach | Sidecar? |
|------|---------------------|---------------|----------|
| Istio (sidecar) | +166% | Per-pod Envoy | Yes |
| Istio Ambient | +8% | ztunnel + waypoint | No (per-node) |
| Linkerd | +33% | Rust micro-proxy | Yes (small) |
| Cilium | +99% | eBPF + per-node Envoy for L7 | No |

Surrogate-1 should default to Linkerd for simplicity, Istio Ambient or Cilium for scale.

### 9.6 Chaos engineering (resilience verification)

| Tool | Scope |
|------|-------|
| **AWS FIS** | EC2/RDS/EKS/ECS native faults |
| **Chaos Mesh** | K8s-native (CNCF), pod/network/IO fault |
| **LitmusChaos** | K8s-native (CNCF), ChaosHub experiments, Helm install |
| **Gremlin** | SaaS, broad coverage, enterprise UX |
| **Steadybit** | SaaS, "reliability scenarios" |
| **Chaos Toolkit** | OSS framework, multi-cloud |

GameDay = scheduled, hypothesis-driven chaos test. Hypothesis -> experiment -> rollback plan -> blast-radius limits -> abort criteria.

---

## 10. Cloud-Specific SRE Knowledge

### 10.1 AWS Well-Architected -- Reliability Pillar (REL)

12 design principles, summarized:
- Design for failure (REL10): multi-AZ minimum, multi-region for tier-1.
- Recovery (REL13): backup/restore, pilot-light, warm-standby, multi-site active/active.
- Failover (REL11): Route 53 health checks + Application Recovery Controller (ARC) for control-plane-independent failover. Global Accelerator for anycast.
- Scaling (REL7): elastic, predictive (Auto Scaling Predictive Scaling).
- Throttling and back-off (REL5): exponential backoff + jitter; circuit breakers.

DR strategy comparison:

| Strategy | RPO | RTO | Cost | Complexity |
|----------|-----|-----|------|-----------|
| Backup & Restore | hours | hours | $ | Low |
| Pilot Light | minutes | tens of min | $$ | Med |
| Warm Standby | seconds | minutes | $$$ | Med-High |
| Multi-site active/active | ~0 | ~0 | $$$$ | High |

### 10.2 GCP CRE (Customer Reliability Engineering)

- Same SRE doctrine as Google internal.
- SLO management built into Cloud Operations.
- Anthos for hybrid; GKE Autopilot for managed K8s.

### 10.3 Azure Well-Architected Framework (WAF)

5 pillars: Reliability, Security, Cost Opt, Operational Excellence, Performance Efficiency. WAF Reliability spec includes "metrics-driven SLOs" as a core practice (2024+).

### 10.4 Multi-cloud DR / failover

- Active-active across cloud providers is rare and expensive (data egress cost dominates).
- Realistic: primary in one cloud, DR in another for *strategic* resilience (CSP-level failure or regulatory).
- DNS for failover: NS1, Cloudflare, Route 53, Azure Traffic Manager. Latency-based routing or health-check failover.
- Data: streaming replication via Kafka MirrorMaker / AWS DMS / GCP Database Migration Service.

---

## 11. Training Data Sources for SRE

### 11.1 Public postmortem corpora

| Source | Volume | Quality |
|--------|--------|---------|
| **danluu/post-mortems** (GitHub) | ~12K stars; 100+ curated, categorized | High; named incidents from FAANG + others |
| **Cloudflare blog** (post-incidents) | 50+ deep dives | Highest technical depth |
| **AWS Post-Event Summaries** (PES) | ~30+ canonical regional events | Authoritative |
| **GCP status / postmortems** (status.cloud.google.com) | dozens | Good |
| **GitHub blog (engineering)** | dozens | Good |
| **Azure status history** | dozens | Variable |
| **Github engineering** | recent outages | Good |
| **Atlassian, Slack, Stripe blogs** | scattered | Mixed |

### 11.2 Open runbook corpora

- **Scoutflo SRE Playbooks** (GitHub, 2025+): 376 playbooks (157 AWS, 194 K8s, 25 Sentry). Structured: title, meaning, impact, 8-10 numbered steps, diagnosis. Free.
- **Tracer-Cloud/opensre**: OSS toolkit for AI SRE agents.
- **Kubernetes-sigs runbooks** (per project): kube-prometheus, kube-state-metrics each ship runbook stubs.
- **runbooks.com** (PagerDuty curated, paid).
- **awesome-runbooks** GitHub lists.

### 11.3 Conference talks / transcripts (high-leverage corpus)

- **SREcon (USENIX)** -- talks 2014-2025, open transcripts on usenix.org. SREcon25 EMEA topics: AI-system reliability, toil reduction, large-scale chaos, follow-the-sun on-call.
- **KubeCon CNCF** -- talks transcribed, K8s-specific.
- **AWS re:Invent** -- talks on YouTube; transcripts via auto-caption.
- **Velocity** (O'Reilly, archived).

### 11.4 Books / canonical references

- *Site Reliability Engineering* (Google, 2016) -- free at sre.google/books -- 27 chapters.
- *The SRE Workbook* (Google, 2018) -- free at sre.google/workbook -- practical chapters on SLO, alerting, on-call.
- *Building Secure & Reliable Systems* (Google, 2020).
- *Implementing Service Level Objectives* (Alex Hidalgo, O'Reilly).
- *Seeking SRE* (David Blank-Edelman, ed.).
- *Database Reliability Engineering* (Campbell & Majors).
- Honeycomb's *Observability Engineering* (Majors, Fong-Jones, Miranda).

### 11.5 LLM-AIOps research corpus (papers Surrogate-1 should ingest)

- [NeurIPS 2025] **STRATUS** -- multi-agent autonomous cloud reliability.
- [MLSys 2025] **AIOpsLab** -- holistic eval framework.
- [ICML 2025] **ITBench** -- IT automation eval.
- [ASE 2025] **Triangle** (Microsoft) -- multi-LLM incident triage.
- [ASE 2025] **iKnow** -- intent-guided RAG chatbot for cloud ops.
- [ASE 2024] **FAIL** -- analyzing software failures from news using LLMs.
- [ICSE-SEIP 2024] **FaultProfIT** -- hierarchical fault profiling.
- [ISSRE 2025] **Empirical Study of Production Incidents in GenAI Cloud Services**.
- Curated index: github.com/Jun-jie-Huang/awesome-LLM-AIOps.

### 11.6 Synthesized SFT examples (what to feed Surrogate-1)

For each capability, generate (prompt, action, rationale) tuples:

```json
{
  "task": "Diagnose K8s pod CrashLoopBackOff",
  "context": {
    "alert": "CrashLoopBackOff in deployment/checkout-api pod-7 in ns prod",
    "kubectl_describe": "Last State: Terminated, Exit Code: 137, Reason: OOMKilled",
    "memory_limit": "512Mi",
    "memory_request": "256Mi",
    "recent_deploys": ["v3.5.0 14:02"]
  },
  "thought": "Exit 137 = SIGKILL by OOM killer. Limit is 512Mi. Either bump limit or fix leak.",
  "action": [
    {"tool": "kubectl", "args": "top pod -l app=checkout-api -n prod"},
    {"tool": "kubectl", "args": "logs deployment/checkout-api -n prod --previous --tail=200"}
  ],
  "verify": "If memory plateau ~480Mi at steady state -> raise to 768Mi. If sawtooth (leak) -> rollback v3.5.0.",
  "citation": ["docs.k8s.io/concepts/configuration/manage-resources-containers/", "runbook:checkout-api-oom"]
}
```

Same structure for: ImagePullBackOff, PVC Pending, OOMKilled, HPA failing, region failover, certificate expiry, cert-manager renewal failure, AlertManager silence misconfig, SLO burn-fix, Postgres replica lag, Redis OOM, S3 5xx, Lambda cold start, ALB target unhealthy, ECS task placement failure, EKS cluster autoscaler failing.

Target dataset shape: 10-30K (incident, action, citation) tuples covering K8s + AWS + GCP + observability tools.

---

## 12. Eval Strategy + Benchmarks

### 12.1 Existing benchmarks

| Benchmark | Domain | Tasks | Notes |
|-----------|--------|-------|-------|
| **AIOpsLab** (MLSys 2025) | Cloud ops | autonomous detect/localize/mitigate | **PRIMARY for Surrogate-1** |
| **ITBench** (ICML 2025) | IT automation | diverse | secondary |
| **Terminal-Bench v2** | Linux shell tasks | 89 tasks | shell tool use |
| **OSWorld** | Desktop GUI | 369 | GUI agent (less SRE) |
| **AgentBench** (ICLR 2024) | 8 envs | broad | foundation eval |
| **SWE-bench** | Code-fix from GH issues | 2.3K | code more than ops |

### 12.2 Surrogate-1 SRE eval dimensions

Build a custom eval combining:

1. **PromQL fluency** -- given metric description + target SLI, write the query. 100 prompts.
2. **K8s diagnostic** -- given `kubectl describe / events / logs`, identify cause and propose action. 200 cases (real anonymized incidents).
3. **SLO definition** -- given service desc, write OpenSLO YAML; auto-grade by Sloth compile.
4. **Alert rule generation** -- given SLO, generate multi-window multi-burn rules; auto-grade by `promtool check rules`.
5. **Postmortem authoring** -- given incident transcript, produce postmortem; LLM-judge by template completeness.
6. **Runbook execution** -- given runbook + tools (kubectl/argo/ssm), execute and verify SLI; auto-grade in sandbox.
7. **Cost trade-off reasoning** -- given workload spec, choose Spot vs On-demand vs Savings Plan. Score against cost calc.

Aggregate to "SRE Capability Score" 0-100.

### 12.3 Hallucination-specific eval

- **Citation grounding rate**: % of factual claims with valid citation (CW/Prom/log link).
- **False action rate**: % of mutating actions that would have been wrong (run in dry-run sandbox).
- **Refuse-when-uncertain rate**: when context is insufficient, model must escalate. Target >95%.

### 12.4 Adversarial telemetry eval

Inject malicious log lines / fake metric labels (the "AI Oops" attack vector). Surrogate-1 must:
- Cross-check across signal types.
- Refuse to act if signals disagree.
- Flag suspected manipulation.

---

## 13. v2 Plan -- How Surrogate-1 Learns SRE

### 13.1 Datasets to collect / synthesize

| Dataset | Size target | Source |
|---------|-------------|--------|
| Public postmortems (clean, structured) | 1K | danluu + Cloudflare/AWS/GCP/GitHub blogs |
| K8s troubleshooting traces | 5K | Scoutflo playbooks + synthetic |
| PromQL / LogQL queries (paired with intent) | 3K | docs + curated |
| OpenSLO YAML examples (paired with service desc) | 1K | Sloth + Pyrra examples + synthetic |
| Alert rule pairs (SLO -> rule) | 1K | Sloth-generated |
| Runbook (trigger -> steps -> verify) | 5K | Scoutflo + synthetic + AWS SSM Automation library |
| Tool-use traces (kubectl/argo/awscli/gcloud) | 10K | sandboxed rollouts + agent rollouts |
| Postmortem transcript -> structured doc | 2K | synthetic from public incidents |
| Cost trade-off examples | 500 | AWS pricing + workload archetypes |

Total: ~28-35K SFT examples + ~5K DPO/RLHF preference pairs (better postmortem vs worse, etc.).

### 13.2 Training stages (LoRA on Qwen2.5-Coder-7B)

1. **Continued pretraining** on SRE-text corpus (Google SRE Book, Workbook, Honeycomb book, conference transcripts) -- 200M tokens, low LR, no instruction format.
2. **SFT** on the 28-35K curated dataset above. Multi-turn for tool-use traces.
3. **Tool-use RL** in a K8s sandbox (kind cluster + pre-baked failure scenarios). Reward = SLI-back-to-baseline + min steps + no destructive action without approval flag.
4. **DPO** on postmortem / runbook quality preferences.
5. **Adversarial fine-tune**: inject misleading telemetry, reward refuse-and-escalate.

### 13.3 Target SRE benchmark

**Primary**: AIOpsLab (MLSys 2025).
**Secondary**: ITBench, internal Surrogate-1 SRE eval suite (see 12.2).
**Hallucination**: custom eval with citation-rate, false-action-rate, refuse-rate metrics.

Initial bar: match GPT-4o-class on AIOpsLab detection+localization tasks with 7B model + LoRA. Stretch: match Claude Sonnet on mitigation.

### 13.4 Tools Surrogate-1 must learn

Mandatory:
- `kubectl` (full noun-verb matrix)
- `helm`, `kustomize`
- `aws` CLI (EC2, EKS, RDS, S3, CloudWatch, IAM, Route 53, SSM)
- `gcloud`, `az` (lighter coverage)
- `argo` (rollouts, workflows), `argocd`, `flux`
- `promtool`, PromQL
- `logcli` (Loki) / Honeycomb query
- `pdcli` / incident.io API
- `terraform` / `cdk` (read; mutating only via PR not direct apply)

ChatOps surface:
- Slack: `/incident open`, `/runbook run`, `/silence add`
- PagerDuty: ack/resolve/escalate via API.

### 13.5 Safety boundary (hard rule)

Surrogate-1 may execute autonomously:
- Read-only diagnostic tools (kubectl get/describe/logs, PromQL queries, log searches).
- Idempotent low-risk fixes IF policy explicitly allows (rolling restart of a single deployment that has a pending fix).

Surrogate-1 must escalate (require human approval) for:
- Any mutation that crosses a service boundary (failover, traffic routing).
- Any deletion of state (DB ops, PV ops, namespace deletion).
- Any infrastructure-level change (Terraform apply, IAM change).
- Any action with cost impact > $10/run.

### 13.6 Continuous learning loop

After every real incident:
1. Auto-extract structured postmortem.
2. Tag (action -> outcome) pairs.
3. Add to SFT corpus.
4. Re-train monthly (LoRA delta).
5. Re-run eval suite; gate release on no regression.

### 13.7 Deliverables for v2

- SRE corpus pipeline (`tools/sre_corpus/`) -- ingestion of postmortems + runbooks + transcripts -> normalized JSONL.
- Sandbox eval harness (`eval/sre_lab/`) -- kind cluster + injected failures, tool-use API.
- LoRA-trained checkpoint `surrogate1-sre-v2.lora`.
- AIOpsLab + custom-eval scorecard.
- Safety policy file (machine-readable allow/deny matrix).

---

## Appendix A. Reference SLI catalog

```yaml
# AVAILABILITY -- HTTP service
sli: ratio
good: rate(http_requests_total{code!~"5..|429",path!~"/(health|metrics)"}[5m])
total: rate(http_requests_total{path!~"/(health|metrics)"}[5m])

# LATENCY -- p95 < 300ms
sli: ratio
good: rate(http_duration_seconds_bucket{le="0.3"}[5m])
total: rate(http_duration_seconds_count[5m])

# DURABILITY -- object store
sli: 1 - (rate(s3_lost_objects_total[1h]) / rate(s3_writes_total[1h]))

# FRESHNESS -- pipeline
sli: data_age_seconds_max < 60

# CORRECTNESS -- ML pipeline
sli: model_accuracy_score >= 0.92

# THROUGHPUT -- queue worker
sli: rate(jobs_processed_total[5m]) >= 100
```

## Appendix B. Quick PromQL cheats

```promql
# Rate of 5xx
sum(rate(http_requests_total{code=~"5.."}[5m])) by (service)

# Error ratio
sum(rate(http_requests_total{code=~"5.."}[5m])) by (service)
/
sum(rate(http_requests_total[5m])) by (service)

# p99 latency (multi-instance aggregation must keep `le`)
histogram_quantile(0.99, sum by (le, service) (rate(http_duration_seconds_bucket[5m])))

# Memory saturation
max by (pod) (container_memory_working_set_bytes / container_spec_memory_limit_bytes) > 0.9

# CPU saturation (throttling)
sum(rate(container_cpu_cfs_throttled_periods_total[5m])) by (pod)
/
sum(rate(container_cpu_cfs_periods_total[5m])) by (pod)

# Predict disk full in 4h (linear regression)
predict_linear(node_filesystem_avail_bytes[1h], 4*3600) < 0
```

## Appendix C. CloudWatch composite alarm (auto-failover trigger)

```yaml
AlarmName: checkout-region-degraded
AlarmRule: |
  ALARM("checkout-5xx-rate-pri")
  AND
  ALARM("checkout-latency-p99-pri")
  AND NOT
  ALARM("aws-region-control-plane-pri")
ActionsEnabled: true
AlarmActions:
  - arn:aws:sns:us-east-1:...:incident-sns
  - arn:aws:lambda:us-east-1:...:auto-failover-route53
```

## Appendix D. References (selected)

- sre.google/books -- Google SRE Book + Workbook (free)
- sre.google/workbook/alerting-on-slos/ -- canonical multi-window multi-burn-rate
- github.com/slok/sloth -- Sloth SLO generator
- github.com/pyrra-dev/pyrra -- Pyrra SLO platform
- openslo.com -- OpenSLO spec
- github.com/danluu/post-mortems -- public postmortem corpus
- github.com/Jun-jie-Huang/awesome-LLM-AIOps -- LLM-AIOps paper index
- github.com/Scoutflo/Scoutflo-SRE-Playbooks -- 376 SRE playbooks
- response.pagerduty.com -- PagerDuty Incident Response docs
- postmortems.pagerduty.com -- PD postmortem culture + template
- charity.wtf -- Charity Majors (observability)
- arxiv 2508.06394 -- "When AIOps become AI Oops" (telemetry adversarial attacks)
- AIOpsLab (MLSys 2025), ITBench (ICML 2025), STRATUS (NeurIPS 2025), Triangle (Microsoft, ASE 2025)
