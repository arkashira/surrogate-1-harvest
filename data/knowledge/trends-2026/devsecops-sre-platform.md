---
name: DevSecOps + SRE + Platform Engineering Trends 2026
description: Shift-left security, SLO practices, IDPs, GitOps, observability
tags: [trends, devsecops, sre, platform-engineering, gitops, observability, 2026]
last_updated: 2026-04-18
---

# DevSecOps + SRE + Platform Engineering Trends 2026

Consolidated landscape brief for 2026: security posture, reliability practices, developer platforms, delivery, observability, incident response, runtime defense, and service mesh. All claims cited inline.

---

## 1. DevSecOps 2026 — Supply Chain, SBOM, SLSA, Shift-Smart

### Shift-Left → Shift-Smart
- 2026 narrative reframes "shift left" as "shift smart": context-aware, low-noise, in-IDE feedback replaces developer-overloading scanners. [Practical DevSecOps](https://www.practical-devsecops.com/devsecops-trends-2026/)
- AI copilots gradually give way to autonomous security agents that triage, prioritize and file fixes. [yoursky.blue](https://yoursky.blue/articles/devsecops-trends)

### Market reality
- 36% of orgs use DevSecOps in 2026 (up from 27% in 2020); 60% of rapid teams embed security (up from 20% in 2019); 75% use AI/ML for code review (up from 41%). [Practical DevSecOps stats](https://www.practical-devsecops.com/devsecops-statistics-2026/)
- 48% of security pros admit their org is behind on SBOM mandates — regulatory pressure (CRA, US EO) is the main driver. [DevDiligent](https://devdiligent.com/blog/future-open-source-security-devsecops-sbom-2026/)

### SBOM → PBOM → operational
- Static SBOM documents are being replaced by continuously queryable systems; Provenance BOMs (PBOMs) prove *how* software was built, not just *what*. [Cloudsmith 2026 Guide](https://cloudsmith.com/blog/the-2026-guide-to-software-supply-chain-security-from-static-sboms-to-agentic-governance)
- SBOMs now stream into vulnerability correlation engines (EPSS + VEX) for real-time blast-radius analysis. [OX Security](https://www.ox.security/blog/application-security-trends-in-2026/)

### SLSA + Sigstore baseline
- SLSA v1.1 + in-toto ITE-6 envelopes are the common currency for provenance claims. [SLSA FAQ](https://slsa.dev/spec/v1.1/faq)
- Keyless signing with Sigstore (Fulcio + Cosign + Rekor) using OIDC short-lived certs is the default pattern; most teams reach SLSA L2 in a day via `slsa-github-generator` or `cosign attest`. [Sigstore docs](https://docs.sigstore.dev/cosign/verifying/attestation/), [AquilaX](https://aquilax.ai/blog/supply-chain-artifact-signing-slsa)
- Cosign-signed SBOMs (CycloneDX/SPDX) attached to OCI artifacts are now table stakes. [Chainguard Academy](https://edu.chainguard.dev/open-source/sigstore/cosign/how-to-sign-an-sbom-with-cosign/)

---

## 2. SRE 2026 — SLI/SLO, Error Budgets, Burn Rate

### Core evolution
- Burn rate alerting replaces threshold alerting: fast-burn (14.4x over 1h = 2 days to exhaustion) + slow-burn (3x over 6h = 10 days) is now canonical. [Google SRE Workbook](https://sre.google/workbook/alerting-on-slos/), [OneUptime](https://oneuptime.com/blog/post/2026-02-20-sre-error-budgets/view)
- Multi-window / multi-burn-rate (MWMBR) patterns cut pager fatigue dramatically versus static SLO alerts. [New Relic](https://newrelic.com/blog/observability/alerts-service-levels-error-budgets)

### Error budget policy as enforcement
- Tiered policy (green >50%, yellow 20-50%, orange 1-20%, red 0% → feature freeze) is codified and automated via GitOps. [Google SRE EBP](https://sre.google/workbook/error-budget-policy/)
- Budget exhaustion halts all changes except P0/security fixes for the 4-week window. [Calmops SRE 2026](https://calmops.com/software-engineering/site-reliability-engineering-sre-principles/)

### 2026 shifts
- SLO-as-code (OpenSLO, Sloth, Pyrra) generates Prometheus recording + alerting rules from YAML specs. [SRE School](https://sreschool.com/blog/error-budget/)
- Chaos engineering embedded into CI — Litmus, Chaos Mesh, AWS FIS triggered on every release candidate.
- Adaptive SLOs: burn-rate targets auto-tuned from historical traffic percentiles.

---

## 3. Platform Engineering 2026 — IDPs, Golden Paths, DORA

### Adoption is mainstream
- 55-90% of orgs run an IDP (depending on survey); Gartner forecasts 80% by 2026. [N-iX](https://www.n-ix.com/platform-engineering-trends/), [DEV Community](https://dev.to/meena_nukala/platform-engineering-in-2026-the-numbers-behind-the-boom-and-why-its-transforming-devops-381l)
- 76% of orgs have a dedicated platform team; IDPs drive 30-50% faster deployments and ~40% developer productivity gains. [DORA](https://dora.dev/capabilities/platform-engineering/)

### Portal wars: Backstage vs Port vs Cortex
- Backstage holds ~89% IDP market share (CNCF graduated). [Roadie 2026](https://roadie.io/blog/platform-engineering-in-2026-why-diy-is-dead/)
- Port / OpsLevel / Cortex win the "low-code" segment — 80% of Backstage value at a fraction of build cost; "DIY Backstage is dead" for small/mid orgs. [Roadie](https://roadie.io/blog/platform-engineering-in-2026-why-diy-is-dead/)
- Roadie + Spotify Portal (hosted Backstage) dominate the managed tier. [Platform Engineering](https://platformengineering.org/blog/platform-engineering-tools-2026)

### Golden paths + DORA
- Golden paths = opinionated, self-service routes for common tasks (new service, DB, env). Centralizes tooling choices once. [Jellyfish](https://jellyfish.co/library/platform-engineering/golden-paths/)
- DORA 4 (Deploy Frequency, Lead Time, CFR, MTTR) remain gold standard; combined with SPACE for DevEx. [DORA Metrics 2026](https://www.programming-helper.com/tech/dora-metrics-2026-software-delivery-performance)
- DORA's 2025 data: top platform capability correlated with DevEx = "clear feedback on task outcomes". [DORA](https://dora.dev/capabilities/platform-engineering/)
- "DORA is not enough" argument: elite teams layer cognitive load metrics, flow efficiency, and SPACE-based DevEx signals. [Oobeya](https://www.oobeya.io/blog/dora-metrics-not-enough-2026)

### The BACK Stack
- Emerging reference architecture: **B**ackstage + **A**rgoCD + **C**rossplane + **K**yverno for end-to-end IDP. [BACK Stack repo](https://github.com/wnqueiroz/platform-engineering-backstack)

---

## 4. Policy as Code — OPA, Kyverno, Cedar

### Tool landscape
- **OPA / Rego**: general-purpose, polyglot, Kubernetes-via-Gatekeeper. Steeper learning curve. [Plural.sh](https://www.plural.sh/blog/open-policy-agent-vs-kyverno/)
- **Kyverno**: Kubernetes-native, YAML policies, now also emits CEL-based ValidatingAdmissionPolicy (VAP) and MutatingAdmissionPolicy (MAP) to use native K8s enforcement. [Kyverno repo](https://github.com/kyverno/kyverno)
- **AWS Cedar**: fine-grained, context-aware authorization language; powers Amazon Verified Permissions. [Spacelift PaC 2026](https://spacelift.io/blog/policy-as-code-tools)

### 2026 patterns
- Hybrid Kyverno + OPA stacks: Kyverno for K8s admission + mutation, OPA for non-K8s (Terraform, APIs, CI). [OneUptime](https://oneuptime.com/blog/post/2026-02-09-policy-as-code-kyverno-opa/view)
- Sigstore image-verification policies via Kyverno are default in regulated orgs. [Kyverno Sigstore](https://main.kyverno.io/docs/policy-types/cluster-policy/verify-images/sigstore/)
- Policy-as-code fused with ArgoCD — pre-sync hooks reject non-compliant manifests. [OneUptime ArgoCD+Kyverno](https://oneuptime.com/blog/post/2026-02-26-argocd-policy-as-code-kyverno/view)

---

## 5. GitOps — ArgoCD vs Flux, Progressive Delivery

### Adoption
- ArgoCD runs 60% of reported K8s clusters per 2025 CNCF survey; 97% production usage (up from 93% in 2023). [CNCF via Northflank](https://northflank.com/blog/flux-vs-argo-cd)
- FluxCD remains strong for resource-constrained, CLI-first orgs; no UI, lower footprint (no repo-server/Redis). [OneUptime 2026](https://oneuptime.com/blog/post/2026-02-26-argocd-vs-fluxcd-2026/view)

### Architecture split
- ArgoCD: centralized, UI + CLI, `Application` CRD. [DEV 2026 Analysis](https://dev.to/mechcloud_academy/the-gitops-standard-in-2026-a-comparative-research-analysis-of-argocd-and-fluxcd-46d8)
- Flux: distributed, Git-native reconciliation, tighter image-automation. [Flux](https://fluxcd.io/)
- Hybrid models emerging — push for velocity, pull for safety/drift. [CNCF GitOps 2025](https://www.cncf.io/blog/2025/06/09/gitops-in-2025-from-old-school-updates-to-the-modern-way/)

### Progressive delivery
- **Argo Rollouts**: canary + blue/green with analysis templates; UI-driven; winner for release engineering with visual control. [Tasrie 2026](https://tasrieit.com/blog/argocd-vs-flux-gitops-comparison-2026)
- **Flagger** (Flux side): decoupled, mesh-native (Istio/Linkerd/NGINX), traffic-shift-based canaries.
- 2026 pattern: Argo Rollouts + Prometheus metric analysis + automatic rollback on SLO burn. [Spacelift GitOps Tools](https://spacelift.io/blog/gitops-tools)

---

## 6. Observability — OpenTelemetry, eBPF, Grafana LGTM

### OTel is the standard
- 2026 question shifted from "should we use OTel" to "why haven't we" — 57% use OTel for metrics, 50% traces, 48% logs. [Grafana Labs 2026](https://grafana.com/blog/2026-observability-trends-predictions-from-grafana-labs-unified-intelligent-and-open/)
- 77% of respondents rank open-source/open-standards as important to observability strategy. [Grafana Open Standards](https://grafana.com/blog/observability-survey-OSS-open-standards-2026/)

### eBPF + zero-code instrumentation
- Grafana donated Beyla to OTel as **OpenTelemetry eBPF Instrumentation (OBI)** — zero-code auto-instrumentation for any Linux workload. [Grafana + OTel 2026](https://grafana.com/blog/opentelemetry-and-grafana-labs-whats-new-and-whats-next-in-2026/)
- **Pixie** (CNCF) uses eBPF for kernel-level network, syscall, DB query capture — no agent code needed. Crucial for legacy/closed-source apps. [IBM Observability](https://www.ibm.com/think/insights/observability-trends)

### LGTM stack (Grafana)
- Loki (logs) + Grafana + Tempo (traces) + Mimir (metrics) = reference OSS stack; sometimes called PLTG. [Medium 2026 Guide](https://medium.com/@krishnafattepurkar/building-a-production-ready-observability-stack-the-complete-2026-guide-9ec6e7e06da2)
- Unified query via Grafana datasource + Faro for RUM closes the loop frontend → backend.

### AI-driven observability
- LLM-based anomaly detection and query assistants now bundled in Datadog Bits, Grafana AI, Dynatrace Davis CoPilot. [Motadata](https://www.motadata.com/blog/observability-trends/)

---

## 7. Incident Management — AI RCA, Postmortems

### AI incident platforms
- Top 2026 AI-native platforms: **Rootly, incident.io, PagerDuty, FireHydrant, Squadcast**. [incident.io](https://incident.io/blog/5-best-ai-powered-incident-management-platforms-2026), [Rootly](https://rootly.com/sre/top-5-ai-powered-incident-management-platforms-2026)
- "AI SRE" teammate pattern: correlates deploys + errors + traces to auto-suggest root cause. [Rootly Auto-Detect](https://rootly.com/sre/rootly-ai-auto-detects-incident-root-causes-in-seconds)
- Teams using AI incident tooling report 17.8% avg MTTR reduction; top implementations hit 30-70%. [OpenObserve](https://openobserve.ai/blog/ai-incident-management-reduce-mttr/)

### PagerDuty specifics
- Strong alerting + on-call; AI gated behind paid add-ons (AIOps, Copilot, generative postmortem drafting). [incident.io review](https://incident.io/blog/5-best-ai-powered-incident-management-platforms-2026)
- Jeli (acquired) powers deep post-incident review / blameless postmortems. [PagerDuty Jeli](https://support.pagerduty.com/main/docs/post-incident-reviews-and-postmortems)

### Postmortem trends
- LLM auto-drafted postmortems from timeline + Slack + git history; human reviews and signs off.
- Blameless + systems-thinking (STAMP, Cynefin) remain the cultural baseline. [OneUptime RCA Reality](https://oneuptime.com/blog/post/2026-03-28-how-ai-is-actually-changing-incident-response/view)

---

## 8. Container / Runtime Security — Falco, Tetragon, Cilium

### Falco (CNCF Graduated)
- Syscall + K8s audit monitoring; **detect-and-alert only** — does not block. [Falco](https://falco.org/)
- Rules-as-code (YAML); integrates with Falcosidekick → Slack/OpsGenie/S3.

### Tetragon (Cilium)
- eBPF-based **observability + real-time enforcement** — can kill processes or deny syscalls before they complete. [Tetragon repo](https://github.com/cilium/tetragon), [InfoWorld](https://www.infoworld.com/article/3810607/tetragon-extending-ebpf-and-cilium-to-runtime-security.html)
- Kubernetes-aware tracing policies; operates at multiple kernel hook layers.
- Lower overhead than Falco for deep tracing. [OneUptime eBPF Security](https://oneuptime.com/blog/post/2026-01-07-ebpf-security-monitoring-falco-tetragon/view)

### 2026 pattern
- **Falco for detection + Tetragon for enforcement** is the recommended combo in regulated K8s estates.
- eBPF is the "silent power behind cloud-native's next phase". [Cloud Native Now](https://cloudnativenow.com/editorial-calendar/best-of-2025/ebpf-the-silent-power-behind-cloud-natives-next-phase-2/)

---

## 9. Service Mesh + Zero Trust — Istio, Linkerd, Cilium

### mTLS + zero-trust baseline
- All three provide automatic mTLS; Istio uses SPIFFE/SPIRE identity; Linkerd enables mTLS by default zero-config. [Reintech 2026](https://reintech.io/blog/kubernetes-service-mesh-comparison-2026-istio-linkerd-cilium)

### Ambient / sidecarless is mainstream
- **Istio Ambient** production-ready 2026: ztunnel (L4, per-node) + waypoint proxies (L7, per-service). Cuts sidecar overhead dramatically. [LiveWyer Ambient](https://livewyer.io/blog/service-meshes-decoded-is-istio-ambient-worth-it/)
- **Cilium Service Mesh**: sidecarless via eBPF; lowest overhead but compromised eBPF = kernel-level blast radius. [TechTarget](https://www.techtarget.com/searchitoperations/news/365535362/Sidecarless-eBPF-service-mesh-sparks-debate)
- **Linkerd**: ultralight Rust sidecar (linkerd-proxy); simplest to run; still competitive. [Linkerd vs Ambient Benchmarks](https://linkerd.io/2025/04/24/linkerd-vs-ambient-mesh-2025-benchmarks/)

### Perf reality (arXiv mTLS test)
- mTLS latency increase: Istio sidecar +166%, Istio Ambient +8%, Linkerd +33%, Cilium +99%. [arXiv](https://arxiv.org/html/2411.02267v1)
- Ambient + eBPF architectures clearly win on raw latency.

### 2026 selection heuristic
- Low-footprint + simplicity → Linkerd
- Feature-rich + enterprise + SPIFFE → Istio Ambient
- Already running Cilium CNI → Cilium mesh (integrated, lowest overhead)

---

## 10. Trending Repos / Projects (2026)

- **Backstage** (`backstage/backstage`) — ~89% IDP share, CNCF graduated. [Platform Engineering Tools](https://platformengineering.org/blog/platform-engineering-tools-2026)
- **ArgoCD** (`argoproj/argo-cd`) — 60% of K8s clusters, 97% production usage.
- **Crossplane** (`crossplane/crossplane`) — CNCF incubating; IaC via K8s CRDs; core of BACK Stack.
- **Kyverno** (`kyverno/kyverno`) — CNCF graduated; VAP/MAP emission added 2025-2026.
- **Falco** (`falcosecurity/falco`) — CNCF graduated; runtime detection baseline.
- **Tetragon** (`cilium/tetragon`) — eBPF enforcement; rapidly climbing.
- **OpenTelemetry** — fastest-growing CNCF project; absorbed Beyla → OBI.
- **Pixie** (`pixie-io/pixie`) — CNCF incubating; eBPF auto-observability.
- **Tekton** (`tektoncd/pipeline`) — CNCF graduated CI/CD; strong Backstage integration.
- **Sigstore / Cosign** (`sigstore/cosign`) — signing/attestation standard.
- **Istio** (`istio/istio`) — Ambient GA, CNCF graduated.
- **Cilium** (`cilium/cilium`) — CNCF graduated; CNI + mesh + Tetragon.

---

## 11. Actionable Recommendations — Ashira's Pipeline (CodeBuild apse1 → CF deploy apse7)

Context: AWS CodeBuild in ap-southeast-1 builds artifacts that feed CloudFormation deploys into ap-southeast-7 (Bangkok).

### Tier 1 — Quick wins (weeks, not quarters)
1. **SBOM every build**: add `syft` + `grype` stages in CodeBuild buildspec → upload SBOM (CycloneDX) to S3 + attach to artifact. Gate on CRITICAL CVEs. [DevDiligent](https://devdiligent.com/blog/future-open-source-security-devsecops-sbom-2026/)
2. **Sigstore keyless signing in CodeBuild**: use OIDC from CodeBuild to Fulcio, sign container images + CF templates with `cosign sign --identity-token`. Log to Rekor. SLSA L2 in ~1 day. [AquilaX SLSA](https://aquilax.ai/blog/supply-chain-artifact-signing-slsa)
3. **CF template compliance**: run `cfn-guard` + `cfn-lint` in CodeBuild pre-deploy stage; fail-fast on IAM wildcards, public S3, missing encryption.
4. **Deploy provenance**: emit SLSA provenance attestation (`slsa-github-generator` equivalent for CodeBuild via custom script) tying git SHA → artifact hash → deploy.

### Tier 2 — Delivery hardening (quarter)
5. **CF change-set gating**: never `deploy` directly — create change-set, run policy-as-code check (OPA against change-set JSON), then approve-and-execute. Prevents drift + catches surprise mutations.
6. **Cross-region artifact replication**: S3 replication apse1 → apse7 with signature verification on pull (`cosign verify --certificate-identity ...`).
7. **Error-budget gate**: before executing deploy into apse7 prod, query CloudWatch SLO dashboard — if burn rate > 2x over last 1h, pause. [Google SRE MWMBR](https://sre.google/workbook/alerting-on-slos/)
8. **Structured logging baseline**: enforce OTel SDK + AWS Distro for OpenTelemetry (ADOT) across all services; traceId propagated apse1 → apse7 via X-Amzn-Trace-Id + W3C traceparent.

### Tier 3 — Platform uplift (half/year)
9. **Internal Developer Platform**: stand up Backstage (or hosted Roadie) cataloging every CF stack + CodeBuild project + ECR repo. Scorecards for "has SBOM", "has runbook", "has SLO". [Roadie 2026](https://roadie.io/blog/platform-engineering-in-2026-why-diy-is-dead/)
10. **Golden path templates**: Backstage scaffolder templates for "new CF stack apse7", "new Lambda apse7", "new ECS service apse7" — each pre-wired with OTel, SBOM, cosign, SLO definitions.
11. **GitOps for K8s workloads (if any EKS)**: ArgoCD in apse7 reconciling against apse1 git mirror; Argo Rollouts + Prometheus analysis for canary with auto-rollback on burn-rate spike.
12. **Runtime security on EKS/ECS-on-EC2**: Falco (detect) + Tetragon (enforce) DaemonSet; stream to CloudWatch Logs + Grafana. [Cloud Native Now eBPF](https://cloudnativenow.com/editorial-calendar/best-of-2025/ebpf-the-silent-power-behind-cloud-natives-next-phase-2/)
13. **AI incident ops**: wire CloudWatch → PagerDuty/Rootly/incident.io; enable AI RCA + auto-draft postmortem. Aim: 20-40% MTTR reduction. [OpenObserve](https://openobserve.ai/blog/ai-incident-management-reduce-mttr/)
14. **Zero-trust apse7**: if running EKS in apse7, adopt Istio Ambient or Cilium mesh for inter-service mTLS; SPIFFE identity for cross-VPC calls. [arXiv mTLS benchmark](https://arxiv.org/html/2411.02267v1)

### Red flags to avoid
- Do **not** stop at "we have SBOMs" — SBOMs that aren't continuously correlated with EPSS/VEX are theatre. [Cloudsmith](https://cloudsmith.com/blog/the-2026-guide-to-software-supply-chain-security-from-static-sboms-to-agentic-governance)
- Do **not** alert on raw latency/error thresholds — migrate to MWMBR burn-rate.
- Do **not** build Backstage DIY for a 1-2 person platform team — use Roadie/Port. [Roadie](https://roadie.io/blog/platform-engineering-in-2026-why-diy-is-dead/)
- Do **not** bolt AI incident tools on without owning the data pipeline — garbage telemetry in = garbage RCA out.

---

## Sources
- [Practical DevSecOps — 2026 Trends](https://www.practical-devsecops.com/devsecops-trends-2026/)
- [Cloudsmith — 2026 Supply Chain Guide](https://cloudsmith.com/blog/the-2026-guide-to-software-supply-chain-security-from-static-sboms-to-agentic-governance)
- [DevDiligent — SBOM 2026](https://devdiligent.com/blog/future-open-source-security-devsecops-sbom-2026/)
- [yoursky.blue — State of DevSecOps](https://yoursky.blue/articles/devsecops-trends)
- [OX Security — AppSec 2026](https://www.ox.security/blog/application-security-trends-in-2026/)
- [Google SRE Workbook — Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
- [Google SRE — Error Budget Policy](https://sre.google/workbook/error-budget-policy/)
- [OneUptime — SRE Error Budgets 2026](https://oneuptime.com/blog/post/2026-02-20-sre-error-budgets/view)
- [Calmops — SRE 2026](https://calmops.com/software-engineering/site-reliability-engineering-sre-principles/)
- [Roadie — Platform Engineering 2026](https://roadie.io/blog/platform-engineering-in-2026-why-diy-is-dead/)
- [Platform Engineering — Tools 2026](https://platformengineering.org/blog/platform-engineering-tools-2026)
- [N-iX — Platform Engineering Trends](https://www.n-ix.com/platform-engineering-trends/)
- [DORA — Platform Engineering Capability](https://dora.dev/capabilities/platform-engineering/)
- [Jellyfish — Golden Paths](https://jellyfish.co/library/platform-engineering/golden-paths/)
- [Oobeya — DORA not enough 2026](https://www.oobeya.io/blog/dora-metrics-not-enough-2026)
- [Spacelift — Policy-as-Code 2026](https://spacelift.io/blog/policy-as-code-tools)
- [Plural.sh — OPA vs Kyverno](https://www.plural.sh/blog/open-policy-agent-vs-kyverno/)
- [OneUptime — PaC K8s 2026](https://oneuptime.com/blog/post/2026-02-09-policy-as-code-kyverno-opa/view)
- [OneUptime — ArgoCD + Kyverno](https://oneuptime.com/blog/post/2026-02-26-argocd-policy-as-code-kyverno/view)
- [Kyverno repo](https://github.com/kyverno/kyverno)
- [Northflank — Flux vs ArgoCD](https://northflank.com/blog/flux-vs-argo-cd)
- [Tasrie IT — ArgoCD vs Flux 2026](https://tasrieit.com/blog/argocd-vs-flux-gitops-comparison-2026)
- [DEV — GitOps Standard 2026](https://dev.to/mechcloud_academy/the-gitops-standard-in-2026-a-comparative-research-analysis-of-argocd-and-fluxcd-46d8)
- [Spacelift — GitOps Tools 2026](https://spacelift.io/blog/gitops-tools)
- [CNCF — GitOps 2025](https://www.cncf.io/blog/2025/06/09/gitops-in-2025-from-old-school-updates-to-the-modern-way/)
- [Grafana — 2026 Observability Trends](https://grafana.com/blog/2026-observability-trends-predictions-from-grafana-labs-unified-intelligent-and-open/)
- [Grafana — OTel 2026](https://grafana.com/blog/opentelemetry-and-grafana-labs-whats-new-and-whats-next-in-2026/)
- [Grafana — Open Standards 2026](https://grafana.com/blog/observability-survey-OSS-open-standards-2026/)
- [Motadata — Observability Trends](https://www.motadata.com/blog/observability-trends/)
- [IBM — Observability Trends 2026](https://www.ibm.com/think/insights/observability-trends)
- [Rootly — Top AI Incident Platforms](https://rootly.com/sre/top-5-ai-powered-incident-management-platforms-2026)
- [incident.io — Best AI Incident 2026](https://incident.io/blog/5-best-ai-powered-incident-management-platforms-2026)
- [OpenObserve — AI Incident Mgmt](https://openobserve.ai/blog/ai-incident-management-reduce-mttr/)
- [OneUptime — AI in Incident Response](https://oneuptime.com/blog/post/2026-03-28-how-ai-is-actually-changing-incident-response/view)
- [OneUptime — eBPF Security Monitoring](https://oneuptime.com/blog/post/2026-01-07-ebpf-security-monitoring-falco-tetragon/view)
- [Falco](https://falco.org/)
- [Tetragon repo](https://github.com/cilium/tetragon)
- [InfoWorld — Tetragon](https://www.infoworld.com/article/3810607/tetragon-extending-ebpf-and-cilium-to-runtime-security.html)
- [Cloud Native Now — eBPF 2025](https://cloudnativenow.com/editorial-calendar/best-of-2025/ebpf-the-silent-power-behind-cloud-natives-next-phase-2/)
- [Reintech — Service Mesh 2026](https://reintech.io/blog/kubernetes-service-mesh-comparison-2026-istio-linkerd-cilium)
- [LiveWyer — Istio Ambient](https://livewyer.io/blog/service-meshes-decoded-is-istio-ambient-worth-it/)
- [Linkerd vs Ambient Benchmarks](https://linkerd.io/2025/04/24/linkerd-vs-ambient-mesh-2025-benchmarks/)
- [arXiv — Service Mesh mTLS Perf](https://arxiv.org/html/2411.02267v1)
- [TechTarget — Sidecarless Debate](https://www.techtarget.com/searchitoperations/news/365535362/Sidecarless-eBPF-service-mesh-sparks-debate)
- [SLSA v1.1 FAQ](https://slsa.dev/spec/v1.1/faq)
- [Sigstore — Cosign Attestations](https://docs.sigstore.dev/cosign/verifying/attestation/)
- [AquilaX — Sigstore + SLSA](https://aquilax.ai/blog/supply-chain-artifact-signing-slsa)
- [Chainguard — Cosign SBOM signing](https://edu.chainguard.dev/open-source/sigstore/cosign/how-to-sign-an-sbom-with-cosign/)
- [Kyverno — Sigstore verification](https://main.kyverno.io/docs/policy-types/cluster-policy/verify-images/sigstore/)
- [BACK Stack — reference IDP](https://github.com/wnqueiroz/platform-engineering-backstack)
- [Programming Helper — DORA 2026](https://www.programming-helper.com/tech/dora-metrics-2026-software-delivery-performance)
- [Medium — 2026 Observability Stack](https://medium.com/@krishnafattepurkar/building-a-production-ready-observability-stack-the-complete-2026-guide-9ec6e7e06da2)
- [SRE School — Error Budget](https://sreschool.com/blog/error-budget/)
- [New Relic — Error Budgets](https://newrelic.com/blog/observability/alerts-service-levels-error-budgets)
- [PagerDuty Jeli Postmortems](https://support.pagerduty.com/main/docs/post-incident-reviews-and-postmortems)
- [Practical DevSecOps — Statistics 2026](https://www.practical-devsecops.com/devsecops-statistics-2026/)
- [DEV — Platform Engineering Numbers 2026](https://dev.to/meena_nukala/platform-engineering-in-2026-the-numbers-behind-the-boom-and-why-its-transforming-devops-381l)
