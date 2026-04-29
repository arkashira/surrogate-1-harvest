---
title: Cloud + Platform Engineering Deep Research for Surrogate-1 v2
date: 2026-04-29
purpose: Train Surrogate-1 (Qwen2.5-Coder-7B + LoRA) into a SOTA Cloud / Platform Engineer
scope: AWS + GCP + Azure + Edge + IDP + IaC + K8s + FinOps + Multi-cloud DR
---

# Surrogate-1 SOTA Cloud + Platform Engineer Training Plan

This document is the canonical knowledge base used to design the v2 instruction-tuning curriculum for Surrogate-1. The model must, autonomously and end-to-end:

1. Design + provision multi-cloud infrastructure (AWS, GCP, Azure, Cloudflare, Vercel)
2. Author production-grade IaC (Terraform, OpenTofu, CDK, Pulumi, Bicep, Crossplane)
3. Operate Kubernetes platforms (EKS / GKE / AKS) with GitOps + service mesh
4. Build internal developer platforms (Backstage, Port, Score, Humanitec)
5. Handle FinOps lifecycle (Inform / Optimize / Operate, 2025 + Scopes)
6. Execute multi-cloud disaster recovery + global routing
7. Stand up edge/serverless (Cloudflare Workers, Vercel Edge, Lambda@Edge)

The research is organized into 14 verticals. Each section closes with the **training corpus** + **eval target** for the v2 curriculum.

---

## 1. AWS Deep Mastery

### 1.1 Certification Scope (training-data anchors)

| Cert | Code | Topics | Why we mine it |
|------|------|--------|----------------|
| Solutions Architect Associate | SAA-C03 | VPC, EC2, S3, RDS, Lambda, IAM basics | Foundational service catalog |
| Solutions Architect Pro | SAP-C02 | Multi-account, hybrid, migration, DR, cost-resilience | Most question banks for org-complexity |
| DevOps Engineer Pro | DOP-C02 | CI/CD, monitoring, IaC, governance | Pipelines + observability |
| Security Specialty | SCS-C02 | KMS, GuardDuty, Inspector, SCPs, IRSA | Hardening + compliance scenarios |
| Advanced Networking Specialty | ANS-C01 | Transit Gateway, Direct Connect, Cloud WAN | Multi-VPC + hybrid networking |

The SAP-C02 exam validates designing multi-account strategies, hybrid architectures, migration at scale, cost optimization, security, and resilience — exactly the Surrogate-1 scope. The exam has 65 scored + 10 unscored questions, passing score 750/1000.

### 1.2 Well-Architected Framework — 6 pillars (Sustainability added Dec 2021, refreshed Nov 2024)

```
1. Operational Excellence  — IaC, runbooks, observability, post-mortems
2. Security                — IAM, encryption, network, IR
3. Reliability              — RTO/RPO, failover, multi-AZ/multi-region
4. Performance Efficiency   — right-sized compute, modern data services
5. Cost Optimization        — RIs/SPs/Spot/Graviton, lifecycle rules
6. Sustainability           — energy efficiency, region selection, idle cleanup
```

**Lenses** Surrogate-1 must recognize: Serverless, SaaS, Migration, Generative AI, IoT, Hybrid Networking, Financial Services, Streaming Media, ML.

### 1.3 Top 30 services for startup/SaaS workloads

```
Compute      : EC2, Lambda, Fargate, Batch, ECS, EKS, App Runner
Storage      : S3, EFS, FSx, EBS
DB           : RDS (Postgres/MySQL), Aurora, Aurora DSQL, DynamoDB, ElastiCache (Redis/Valkey), OpenSearch
Network      : VPC, Route53, CloudFront, ALB/NLB/GWLB, Transit Gateway, PrivateLink, API Gateway
Identity     : IAM, IAM Identity Center (SSO), Cognito, Organizations, Verified Permissions
Observability: CloudWatch, X-Ray, Managed Prometheus, Managed Grafana, OpenSearch
Security     : KMS, Secrets Manager, GuardDuty, Inspector, Security Hub, WAF, Shield
Data/AI      : Bedrock, SageMaker, Glue, Athena, Kinesis, MSK, Step Functions
Messaging    : SQS, SNS, EventBridge
DevTools     : CodePipeline, CodeBuild, CodeDeploy, CDK, SAM
```

### 1.4 VPC networking patterns

**Hub-and-spoke with Transit Gateway** — TGW is the managed hub-and-spoke service for VPCs and on-prem; centralizes routing without VPN overlays. Aligns with Well-Architected Reliability pillar `REL02-BP04`.

**Centralized PrivateLink endpoints** — Host interface VPC endpoints (e.g., for `s3.api`, `kms`, `sts`, `secretsmanager`) in a single shared-services VPC. All spoke VPCs reach AWS APIs via TGW → endpoint VPC. Saves cost (one $7.30/month-per-AZ endpoint instead of N).

**Decision tree**:

```
Two VPCs, low traffic, no transitive    → VPC Peering
Service consumed across many VPCs       → PrivateLink (endpoint service)
≥3 VPCs with transitive routing needed  → Transit Gateway (hub-and-spoke)
Multi-region + on-prem at scale         → Cloud WAN
```

### 1.5 IAM advanced

**SCP** = guardrail at OU/account level; **deny-by-default**, no permissions granted, only constrains. SCPs evaluated AND'd with IAM policies + permission boundaries — action allowed only when ALL allow it.

**Permission boundary** = max permissions a role/user CAN have, regardless of attached policies. Used for delegated admin (developer can create roles, but only ones bounded).

**ABAC** = attribute-based access control via tags (e.g., `aws:PrincipalTag/team` must equal `aws:ResourceTag/team`). Reduces role count drastically. SCPs can lock the tagging itself so principals can't escalate by re-tagging.

Example SCP — deny untagged production resources:

```json
{
  "Sid": "DenyUntaggedEnvProd",
  "Effect": "Deny",
  "Action": ["ec2:RunInstances", "rds:CreateDBInstance"],
  "Resource": "*",
  "Condition": {
    "StringNotEquals": {
      "aws:RequestTag/Environment": ["prod","staging","dev"]
    }
  }
}
```

### 1.6 Cost optimization (FinOps lever in §11)

**Compute discount tiers** (max savings vs on-demand):

| Mechanism | Max Discount | Flexibility |
|-----------|-------------|-------------|
| Standard RI | 75% | Locked region+family+OS, 1 or 3 yr |
| Convertible RI | 54% | Can change family within OS |
| EC2 Instance SP | 72% | Locked family, any size, any AZ |
| Compute SP | 66% | EC2 + Fargate + Lambda + SageMaker |
| Spot | 90% | Variable interruption (2-min notice) |
| Graviton | +40% perf/$ | ARM64 (must support arch) |

**June 2025 change** — RIs and SPs are restricted to single-end-customer; MSPs can no longer share commitments across accounts.

Surrogate-1 must teach `Compute Optimizer` recommendations + apply them.

### 1.7 AWS-specific tools & CLI surface

```
aws cli      → primary
aws cdk      → preferred IaC (TS/Python). CDK Refactor (Sept 2025) safely renames constructs without replacement
aws sam      → serverless/Lambda focus
aws copilot  → ECS/Fargate (END OF SUPPORT June 12 2026 — migrate to ECS Express or CDK L3)
aws amplify  → frontend + serverless backend, Git-driven CI/CD
```

### 1.8 Training corpus for AWS

```
- AWS Well-Architected docs (all 6 pillar PDFs + 9 lenses)
- AWS official examples: aws-samples/* (8000+ repos)
- terraform-aws-modules/* (vpc 126M downloads, eks 96.3M downloads)
- AWS CDK guide v2 + cdk-patterns/serverless
- SAP-C02 question banks (ExamTopics, Tutorials Dojo)
- AWS Architecture Center reference architectures (multi-account, DR, hybrid)
- Service Control Policy examples: aws-samples/service-control-policy-examples
```

**Eval target**: 75% on a custom AWS-design eval (multi-account VPC + hub-spoke + IAM bootstrap + EKS cluster) with `cfn-lint` + `cfn-guard` passing.

---

## 2. GCP Deep Mastery

### 2.1 Certifications

| Cert | Released | Scope |
|------|----------|-------|
| Cloud Digital Leader | — | Business/strategy |
| Associate Cloud Engineer | — | gcloud + GCE/GKE/GCS basics |
| Professional Cloud Architect (PCA) | refreshed Oct 30 2025 | Design — ~30% net-new content (Vertex AI, Gemini, AI Hypercomputer) |
| Professional Cloud Network Engineer (PCNE) | — | VPC, hybrid, Cloud Interconnect |
| Professional Cloud DevOps Engineer | — | SLO, CI/CD, observability |
| Professional Cloud Security Engineer | — | Org policies, VPC-SC, BeyondCorp |
| Professional Cloud Database Engineer | — | Cloud SQL, AlloyDB, Spanner |

PCA exam covers Compute Engine, Cloud Storage, App Engine, GKE, with the Oct 2025 refresh adding ~30% new content focused on Vertex AI, Gemini integration, and AI Hypercomputer.

### 2.2 GKE advanced

**GKE Autopilot** — Google manages provisioning, scaling, security, add-ons. Bills per-pod resource request (not nodes). Best when team doesn't want to tune nodepools.

**GKE Standard** — Customer-managed nodepools; required for DaemonSets that need privileged hostPath, custom CNI, niche GPU/TPU shapes.

**GKE version ladder** — GKE adopts new K8s versions fastest (~2 weeks). Autopilot gets 30 months extended support; AKS LTS 24 months; EKS Extended Support +12 months.

**Anthos / GKE Enterprise** — Multi-cluster across on-prem + AWS + Azure. Provides Config Sync (GitOps), Service Mesh, Policy Controller. Now folded into GKE Enterprise SKU.

### 2.3 BigQuery + Vertex AI integration (2025)

- `AI.GENERATE`, `AI.GENERATE_TABLE`, `AI.EMBED`, `AI.SIMILARITY` are now **GA** in BigQuery.
- BQML supports Gemini 3.0 for generative SQL functions.
- Vertex AI End User Credentials (2025) lets Vertex models authenticate via the calling user's IAM — no service-account proxy.

This is core for any data-platform engineering Surrogate-1 builds.

### 2.4 Cloud Run + Cloud Functions

- Cloud Run gen2 = container-as-a-service, scales to zero, max 60-min timeout, supports websockets/streaming.
- Cloud Functions gen2 = built ON Cloud Run; choose Functions for trigger-driven, Run for HTTP/services.
- Cloud Run jobs = batch workloads (cron via Cloud Scheduler).

### 2.5 GCP-specific tools

```
gcloud           → primary CLI
Terraform google → official provider, fastest day-1 support for new services
Config Connector → GCP-native Crossplane equivalent (KCC). Manage GCP resources via K8s CRDs
Cloud Deploy     → managed GitOps for GKE
Cloud Build      → CI (yaml + buildpacks)
```

### 2.6 Training corpus for GCP

```
- GCP architecture center (cloud.google.com/architecture)
- terraform-google-modules/* (network, kubernetes-engine, cloud-foundation-fabric)
- Cloud Foundation Fabric (Google's reference org setup)
- gcp-pca-study-guide repos
- Anthos config-management examples
- BQML + Vertex AI codelabs
```

---

## 3. Azure Deep Mastery

### 3.1 Certifications

| Cert | Code | Scope |
|------|------|-------|
| Administrator Associate | AZ-104 | RBAC, IAM, networking, storage, Bicep basics |
| Solutions Architect Expert | AZ-305 | Design — governance, identity, infra, app, integration |
| Security Engineer | AZ-500 | Defender, Sentinel, Conditional Access |
| DevOps Engineer Expert | AZ-400 | Pipelines, IaC, monitoring |

AZ-305 (refreshed April 17 2026) covers: Identity/governance/monitoring, data storage, infrastructure & availability, application architecture, network solutions, data integration, business continuity. Prereq: AZ-104.

### 3.2 Azure compute deep cuts

```
AKS              → managed K8s; "AKS LTS" = 24-mo extended support per minor
App Service      → PaaS web hosting (Plans = Basic/Standard/Premium/Isolated)
Functions        → consumption / premium / dedicated
Container Apps   → CaaS on KEDA (scale-to-zero from events)
Container Instances (ACI) → single-pod throwaway
Virtual Machine Scale Sets (VMSS) → IaaS auto-scaling
Azure Spring Apps → managed Spring Boot
```

### 3.3 Azure DevOps + GitHub Enterprise (Microsoft owns both)

- **Azure DevOps** = Boards + Repos + Pipelines + Artifacts. Mature for .NET-heavy orgs.
- **GitHub Enterprise** + Actions = where new investment is going (Microsoft's strategic direction).
- 2025 trend: most new Azure customers go GitHub-first; Azure DevOps is in maintenance mode.

### 3.4 Azure tooling

```
az cli   → primary
Bicep    → DSL that transpiles to ARM. JSON ARM templates → DEPRECATED for new work
Pulumi   → first-class Azure native provider
Terraform azurerm + azuread → mature, official
```

Bicep simplifies ARM but is Azure-only — for multi-cloud orgs, Terraform remains primary.

### 3.5 Training corpus for Azure

```
- Cloud Adoption Framework (Microsoft's enterprise reference)
- Azure-Samples/* GitHub org
- Azure Verified Modules (AVM) — Microsoft's curated Bicep + Terraform modules
- AZ-305 study guides + Microsoft Learn content
- Azure Architecture Center patterns
```

---

## 4. Multi-Cloud Strategy

### 4.1 Workload portability tools

| Tool | Approach | Best fit |
|------|----------|----------|
| Crossplane | K8s-native control plane → cloud APIs via providers | Platform teams already on K8s |
| Anthos | GCP-managed clusters across clouds + on-prem | GKE-centric orgs wanting unified control |
| Azure Arc | Azure-managed servers/K8s outside Azure | Azure-centric hybrid |
| Terraform | IaC abstraction (provider-per-cloud) | Most common; least lock-in |
| Pulumi | Real code (Python/TS); equivalent provider coverage | Engineering-heavy teams |

### 4.2 Crossplane v2 (Aug 2025)

Major upgrades:

- **Compositions can include any K8s resource** — not just Crossplane MRs. Mix RDSInstance + Deployment + CloudNativePG cluster in one XR.
- **Namespace-first** — XRs and MRs are namespaced by default (was cluster-scoped).
- **Operations** — function pipelines for cert monitoring, rolling upgrades, scheduled maintenance.
- **Multi-cloud status** — AWS providers fully migrated; Azure/GCP/Terraform providers still being updated to v2.

### 4.3 DR / failover patterns

| Pattern | RTO | RPO | Cost (vs single-region) |
|---------|-----|-----|-------------------------|
| Backup & restore | hours-days | hours | 1.0x (storage only) |
| Pilot light | 10s of min | minutes | 1.1-1.3x |
| Warm standby | minutes | minutes | 1.5-1.8x |
| Multi-site active/active | seconds | ~0 | 1.8-2.5x |

Multi-cloud active/active typically costs 1.8–2.5x single-cloud due to duplicate infra + ops overhead. Recommendation: active/passive across clouds + active/active across regions WITHIN primary cloud.

### 4.4 Latency-based routing

```
Route53 latency policy   → AWS-native, cheapest
Cloud DNS geo-routing    → GCP-native
Azure Traffic Manager    → Azure-native
Cloudflare load balancer → multi-cloud
NS1 / Constellix         → enterprise multi-cloud DNS
```

Cloudflare LB is the most common cross-cloud answer because it sits OUTSIDE the providers.

### 4.5 Cost arbitrage

- GPU cost: GCP < AWS < Azure (TPUs are GCP-only and cheaper per FLOP at scale)
- Egress: AWS most expensive; Cloudflare R2 has $0 egress (S3-compatible)
- Object storage: B2 ($6/TB/mo) < R2 ($15) < S3 standard ($23) < GCS standard ($26)
- Reserved discounts: deepest in AWS (75% std RI), shallower in Azure (65%), GCP CUDs ~57%

### 4.6 Vendor lock-in mitigation

```
1. Use OSS data formats (Parquet, Iceberg, Delta) — not proprietary
2. Use OSS DBs (Postgres / Redis-compatible Valkey) — not Aurora-only or Cosmos-only
3. Use OCI containers + K8s — cluster portability via Crossplane/Anthos
4. Use Terraform with multi-provider modules — abstract per-cloud differences
5. Avoid managed-vendor-only auth — use OIDC + Keycloak or Auth0 (cross-cloud)
6. Multi-cloud DNS (Cloudflare/NS1) so Route53/Cloud DNS isn't single point
```

---

## 5. IaC Mastery

### 5.1 Terraform / OpenTofu (post-BSL fork)

- HashiCorp **Terraform OSS under BSL discontinued after July 2025** → OpenTofu is the OSS continuation under Linux Foundation.
- Most TACOS (Spacelift, Env0, Scalr) support both. Most modules still work in both.
- For new orgs in 2026 → **default OpenTofu**.

**Best practices** (2025):

```
1. Remote backend (S3+DynamoDB lock, GCS, Azure Blob) — never local state
2. Split state: per-environment (dev/staging/prod) + per-domain (network, data, compute)
   - Terralith state >50MB causes timeouts; >10MB visible perf hit
3. Module versioning: `~> 2.5` (allow patch+minor, block major)
4. Pre-commit: terraform fmt + validate + tflint + tfsec/checkov
5. CI/CD: Atlantis (OSS, self-host) or Spacelift / Env0 / Scalr / Terramate (SaaS)
6. State locking always on
7. Drift detection: `terraform plan -refresh-only` on schedule (Spacelift / Atlantis cron)
8. Workspaces only for environment isolation; NOT for tenant separation
```

**Workspace anti-pattern**: using workspaces for cust-1, cust-2, cust-3 — should be separate state files / dirs instead. Workspaces good for `dev`, `staging`, `prod` of same module.

### 5.2 CloudFormation

```
- Nested stacks → for >500 resources / cross-stack dependencies
- Custom resources → Lambda-backed for CFN gaps. Use AwsCustomResource (CDK) for single-API-call
- Change sets → preview before apply (mandatory for prod)
- Stack policies → prevent accidental updates to specific resources
- Service Catalog → curated CFN templates exposed to devs
- StackSets → multi-account/multi-region rollout
```

### 5.3 AWS CDK best practices

```
- Constructs L1 (raw CFN) / L2 (curated AWS) / L3 (composite patterns)
- Aspects → enforce policy across all constructs (e.g., "all S3 buckets must encrypt")
  - Aspects run at synth-time → cfn-guard runs post-synth → both = defense in depth
- Don't extend Construct unless interacting with AWS resources directly; helper class is enough
- Custom resources: use AwsCustomResource for single API call; full Lambda-backed for complex
- CDK Refactor (Sept 2025) → safely rename or move resources without replacement
- Pipelines L3 = managed CodePipeline that self-mutates
```

### 5.4 Pulumi

- Real code (TS/Python/Go/.NET/Java) — language loops, classes, unit tests with native frameworks.
- Pulumi onboarding ~30% faster for engineers already knowing TS/Python (vs HCL).
- Day-1 support for new cloud services because Pulumi wraps SDKs directly.
- Pulumi ESC = encrypted env+secrets store; Pulumi Deployments = managed runners.

### 5.5 Crossplane (K8s-native multi-cloud)

```yaml
# Composition that creates RDS + Deployment + Service in one XR
apiVersion: apiextensions.crossplane.io/v2
kind: Composition
metadata:
  name: web-app-with-db
spec:
  compositeTypeRef:
    apiVersion: example.io/v1alpha1
    kind: WebApp
  pipeline:
  - step: provision-db
    functionRef:
      name: function-patch-and-transform
    input:
      apiVersion: pt.fn.crossplane.io/v1beta1
      kind: Resources
      resources:
      - name: rds
        base:
          apiVersion: rds.aws.upbound.io/v1beta1
          kind: Instance
          spec:
            forProvider:
              instanceClass: db.t3.medium
              engine: postgres
              engineVersion: "16"
              allocatedStorage: 50
      - name: deployment
        base:
          apiVersion: apps/v1
          kind: Deployment
          spec:
            replicas: 3
```

### 5.6 IaC TACOS comparison

| Tool | OSS / SaaS | IaC Coverage | Best For |
|------|-----------|--------------|----------|
| Atlantis | OSS, self-host | TF/OpenTofu/Terragrunt | Free, GitHub-PR workflow |
| Spacelift | SaaS + self-hosted | TF/OpenTofu/Terragrunt/Pulumi/CFN/K8s/Ansible | Enterprise multi-IaC |
| Env0 | SaaS only | Multi-IaC + strong FinOps | FinOps-aware deployment |
| Terramate | OSS + SaaS | TF/OpenTofu | Stack orchestration + DAGs |
| Scalr | SaaS + self-hosted | TF/OpenTofu | TFC alternative |
| Terraform Cloud | SaaS | TF only | Default if already HashiCorp |

### 5.7 Real Terraform module example (multi-cloud DRY)

```hcl
# environments/prod/main.tf
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "prod-vpc"
  cidr = "10.0.0.0/16"
  azs  = ["us-east-1a", "us-east-1b", "us-east-1c"]

  private_subnets  = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets   = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
  database_subnets = ["10.0.201.0/24", "10.0.202.0/24", "10.0.203.0/24"]

  enable_nat_gateway     = true
  single_nat_gateway     = false  # one per AZ for HA
  enable_vpn_gateway     = false
  enable_dns_hostnames   = true
  enable_flow_log        = true
  flow_log_destination_type = "cloud-watch-logs"

  tags = local.common_tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "prod-platform"
  cluster_version = "1.32"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_endpoint_public_access = false
  cluster_endpoint_private_access = true

  cluster_addons = {
    coredns                = { most_recent = true }
    kube-proxy             = { most_recent = true }
    vpc-cni                = { most_recent = true }
    aws-ebs-csi-driver     = { most_recent = true }
    eks-pod-identity-agent = { most_recent = true }
  }

  eks_managed_node_groups = {
    system = {
      instance_types = ["t3.medium"]
      min_size = 2
      max_size = 4
      desired_size = 2
      labels = { workload = "system" }
      taints = [{ key = "system", value = "true", effect = "NO_SCHEDULE" }]
    }
    karpenter = {
      instance_types = ["m6g.large"]  # Graviton
      capacity_type  = "ON_DEMAND"
      min_size = 1
      max_size = 2
      desired_size = 1
      labels = { workload = "karpenter" }
    }
  }

  enable_irsa = true
  enable_cluster_creator_admin_permissions = true
}
```

### 5.8 Training corpus for IaC

```
- HashiCorp learn.hashicorp.com/terraform tutorials (1000+ lessons)
- terraform-aws-modules / terraform-google-modules / Azure/terraform-azurerm-* (AVM)
- Pulumi pulumi/examples (1500+)
- aws-samples/aws-cdk-examples
- Crossplane upbound/configurations (reference platforms)
- Awesome-terraform / awesome-pulumi GitHub lists
- IaC-Eval benchmark (academic Terraform benchmark)
- TACOS docs: Spacelift, Env0, Atlantis, Terramate
```

---

## 6. Kubernetes Platform Engineering

### 6.1 Kubernetes 1.32 → 1.35 highlights (2025)

| Version | Released | Key features |
|---------|----------|-------------|
| 1.32 | Dec 2024 | KubeletFineGrainedAuthz; Memory Manager GA; Anonymous Auth Configurable Endpoints |
| 1.33 | Apr 2025 | Sidecars GA; supplementalGroupsPolicy beta; in-place pod resize beta |
| 1.34 | Aug 2025 | DRA core GA (Dynamic Resource Allocation for GPUs/TPUs/FPGAs) |
| 1.35 | Dec 2025 | Fine-grained Supplemental Groups GA; TLS 1.3 baseline |

**Pod Security Standards** are GA since v1.25 (NOT 2025). Three levels: Privileged / Baseline / Restricted, applied via `pod-security.kubernetes.io/<mode>` namespace labels.

### 6.2 Helm vs Kustomize vs Carvel

| Tool | Approach | Strength | Weakness |
|------|----------|----------|----------|
| Helm | Templating + values + chart | Package manager (75% adoption); Helm 4 (Nov 2025) adds server-side apply | Templating debug pain |
| Kustomize | Patch-based overlays on bases | No magic; built into kubectl | No release/version concept; needs ArgoCD/Flux for state |
| Carvel | ytt + kapp + kbld + imgpkg | Strong CI bundling; image relocation | Steeper learning curve, smaller community |

**Mature pattern**: Helm to install upstream charts (Cilium, ArgoCD, Prometheus); Kustomize overlays per environment. Use ArgoCD `helm` source with `valuesObject` overrides.

### 6.3 GitOps — ArgoCD vs FluxCD (2025 reality)

**Weaveworks closed in early 2024** — Flux became fully community-driven (CNCF graduated). ArgoCD has clearer commercial path (Akuity, CodeFresh).

| Aspect | ArgoCD | FluxCD |
|--------|--------|--------|
| UI | Strong native dashboard | None native (use Weave GitOps or third-party) |
| RBAC | Built-in + Projects multi-tenancy | Standard K8s RBAC only |
| Architecture | Hub-and-spoke | Decentralized, K8s-idiomatic |
| Multi-cluster | Native (App-of-Apps, ApplicationSets) | Per-cluster Flux + Notification Controller |
| Best for | Most enterprises in 2025 | Air-gapped / minimal-deps / true GitOps purists |

Default 2026 recommendation: **ArgoCD** for most orgs.

### 6.4 Service Mesh — Istio vs Linkerd vs Cilium (2025)

| Mesh | Sidecars | Data plane | Best fit |
|------|----------|------------|----------|
| Istio | Sidecar OR Ambient (ztunnel + waypoint) | Envoy | Advanced traffic mgmt, deep telemetry |
| Linkerd | Sidecar only | linkerd2-proxy (lightweight Rust) | Simplicity + lowest overhead |
| Cilium | Sidecarless | eBPF + Envoy (L7) | Network policy + perf at scale |

**Memory cost reality**: 500 services on Istio sidecar = ~25–50 GB more RAM than same on Linkerd. Translates to real $$.

**Cilium caveat**: eBPF can't parse HTTP/gRPC or do mTLS termination — Cilium still uses Envoy for L7, so the perf delta vs Istio at L7 is small.

**Decision tree**:

```
Tiny team, just want mTLS + observability     → Linkerd
Already on Cilium CNI, want unified           → Cilium Service Mesh
Need full traffic mgmt (canary, mirror, fault) → Istio Ambient
```

### 6.5 Ingress + Gateway API (the Ingress era is ending)

Ingress-NGINX official **maintenance halt March 2026**. Gateway API is the K8s-official successor.

Gateway API provides:
- Protocol-agnostic (HTTP, TCP, gRPC, TLS passthrough)
- Role-split: GatewayClass (provider) → Gateway (cluster operator) → HTTPRoute (app dev)
- Built-in canary/blue-green via weighted routes
- Both north-south AND east-west

Ingress controllers / Gateway implementations:

| Implementation | Notes |
|----------------|-------|
| Envoy Gateway | Reference implementation; CNCF |
| Istio | Native Gateway API support (replaces Istio VirtualService for new) |
| NGINX Gateway Fabric | NGINX-backed, replaces ingress-nginx |
| Cilium Gateway | CNI-integrated |
| Traefik | Long-time leader for Ingress; Gateway API supported |

Migration: **`ingress2gateway` 1.0** (2026) translates Ingress + annotations → Gateway API resources.

### 6.6 Operators

```
Operator SDK (Red Hat)        → Go/Helm/Ansible scaffolding
Kubebuilder                   → upstream K8s SIG; cleaner Go
KUDO                          → declarative operator definition
metacontroller                → Lua/JSONNET-style hooks (lightweight)
```

When to write an operator: state machine that doesn't fit `Deployment` (e.g., DB clustering, leader election, custom backup).

When NOT: just templating → use Helm/Kustomize.

### 6.7 Multi-cluster — Karmada vs Cluster API vs OCM

**KubeFed is EOL** (no commits since 2020).

| Tool | Approach |
|------|----------|
| Karmada | CNCF Incubation; multi-cluster scheduling + propagation policy. v1.15 (Oct 2025) adds multi-template workload awareness + structured logging |
| Cluster API (CAPI) | Declarative cluster lifecycle (CAPA AWS, CAPG GCP, CAPZ Azure providers) |
| Open Cluster Management (OCM) | Red Hat-led; ACM commercial product |
| Anthos / GKE Enterprise | GCP-managed; folds in Config Sync + Mesh + Policy |
| Azure Arc | Azure-managed; brings Azure Policy/Monitor to any cluster |

Pattern: **CAPI provisions clusters**, **Karmada propagates workloads**, **ArgoCD reconciles config**.

### 6.8 Cost — Kubecost vs OpenCost

- OpenCost (Apache 2.0) — free, single-cluster focus, real-time allocation by pod/namespace/controller, multi-cloud (AWS/GCP/Azure). Now ships with built-in MCP server (2025) for AI agent access.
- Kubecost (IBM-owned post-2024 acquisition) — adds budgets, RBAC, multi-cluster aggregation, automated cost policies. Starts $449/mo, enterprise on quote.

### 6.9 Karpenter + Spot + Graviton

Real customer outcomes:

- Tinybird: 20% AWS bill reduction with EKS+Karpenter+Spot
- Series B SaaS (200 microservices): $52k → $23k/mo (56%) with Graviton mix + Karpenter + Spot
- One reported migration: $50k → $22k/mo Karpenter + Spot + VPA

**NodePool best practices**:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    spec:
      requirements:
      - key: kubernetes.io/arch
        operator: In
        values: ["arm64", "amd64"]  # Graviton preferred but allow x86 fallback
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot", "on-demand"]
      - key: karpenter.k8s.aws/instance-category
        operator: In
        values: ["m", "c", "r"]
      - key: karpenter.k8s.aws/instance-generation
        operator: Gt
        values: ["6"]  # m6g+, c6g+, r6g+
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
  limits:
    cpu: 1000
```

### 6.10 Helm chart example for a service

```yaml
# values.yaml
image:
  repository: ghcr.io/org/api
  tag: ""  # set by ArgoCD Image Updater or via CI
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    memory: 512Mi

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 30
  targetCPUUtilizationPercentage: 70

serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123:role/api-irsa  # IRSA for AWS

podSecurityContext:
  runAsNonRoot: true
  runAsUser: 65532
  fsGroup: 65532

securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault

podDisruptionBudget:
  enabled: true
  minAvailable: 2

networkPolicy:
  enabled: true
  ingress:
  - from:
    - podSelector:
        matchLabels:
          role: gateway
```

### 6.11 Training corpus for K8s

```
- kubernetes/kubernetes (source + design proposals KEPs)
- kubernetes/website (docs)
- helm/charts (deprecated) + bitnami/charts + community charts
- argo-cd/argo-cd repo + examples
- karmada-io/karmada
- cilium/cilium (eBPF code + e2e tests)
- istio/istio
- linkerd/linkerd2
- aws/karpenter-provider-aws
- backstage/backstage source + plugins
- run-x/awesome-kubernetes
- KubeCon talk transcripts (CNCF YouTube; can transcribe via Whisper)
```

**Eval target**: 70% on K8s-Bench (manifest validity + Helm chart that `helm template` validates + ArgoCD Application that syncs + NetworkPolicy that locks down by default).

---

## 7. Internal Developer Platform (IDP)

### 7.1 IDP landscape (2025)

| Tool | Type | Strength | TTV (time-to-value) |
|------|------|---------|---------------------|
| **Backstage** (Spotify, CNCF) | OSS framework, build-it-yourself portal | Most flexible; 120+ Spotify-internal plugins; CNCF | 3–6 months |
| **Port** | Commercial SaaS portal | No-code, fast to deploy | Days |
| **Cortex** | Commercial — service ownership + scorecards | Best for >50-eng orgs needing governance | Weeks |
| **OpsLevel** | Commercial — quality scorecards | Strong dashboards | Weeks |
| **Humanitec** | Platform Orchestrator (NOT a portal) | Backend that resolves Score files into infra | Weeks |
| **Encore** | All-in-one (codegen + infra) | Strong opinionated dev workflow | Days |
| **Cloudomation** | Workflow automation IDP | Low-code for non-K8s orgs | Days |

**Key mental model**: Portal (Backstage/Port) ≠ Orchestrator (Humanitec). You often need BOTH — portal as UI, orchestrator as the backend that creates the actual cloud resources.

### 7.2 Backstage core

```
Catalog          → entities (Component, System, API, Resource, Group, User)
TechDocs         → MkDocs-based, lives next to code
Software Templates → Cookiecutter-style scaffolds (repo + IaC + pipeline + DB)
Search           → indexes catalog + docs
RBAC             → Spotify's RBAC plugin (commercial)
Soundcheck       → Spotify's tech-standards/scorecard plugin (commercial)
Insights         → adoption analytics (commercial)
Cloud Backstage  → managed hosted (commercial)
```

Open-source plus commercial Spotify Portal (RBAC, Insights, Soundcheck) = "production-ready Backstage."

**catalog-info.yaml** example:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: orders-api
  description: Order management service
  annotations:
    github.com/project-slug: org/orders-api
    backstage.io/techdocs-ref: dir:.
    pagerduty.com/integration-key: ${SECRET_PD}
    sonarqube.org/project-key: org_orders-api
    grafana/dashboard-selector: "tags @> 'orders'"
  tags: [java, spring-boot, payments-domain]
spec:
  type: service
  lifecycle: production
  owner: payments-team
  system: payments
  providesApis: [orders-rest-api]
  consumesApis: [users-rest-api]
  dependsOn: [resource:orders-db]
```

### 7.3 Score (CNCF, 2024) — workload spec

Score is platform-agnostic workload spec. Developer writes ONE YAML; platform team's `score-compose` / `score-helm` / `score-k8s` translates.

```yaml
# score.yaml
apiVersion: score.dev/v1b1
metadata:
  name: orders-api
containers:
  api:
    image: ghcr.io/org/orders-api:${TAG}
    variables:
      DATABASE_URL: postgres://${secrets.DB_PASSWORD}@${resources.db.host}/orders
      REDIS_URL: ${resources.cache.url}
    resources:
      requests: { cpu: "100m", memory: "256Mi" }
service:
  ports:
    web: { port: 8080 }
resources:
  db:
    type: postgres
  cache:
    type: redis
  route:
    type: dns
    params: { host: orders.example.com }
```

The platform team configures resource definitions (e.g., `db.postgres → AWS RDS via Crossplane`) — devs don't see/care.

### 7.4 OAM vs Score

OAM is broader (whole-app model with traits + scopes + components); Score is single-workload + simpler. Score is winning in 2025 because of its narrower scope and CNCF backing.

### 7.5 Humanitec orchestrator pattern

```
Developer:    score.yaml in repo
GitOps:       commit → CI → calls Humanitec API
Humanitec:    resolves score against Resource Definitions
              → creates EKS Deployment + RDS + Redis + Route53 record
Platform:     defines Resource Definitions (e.g., postgres → AWS RDS via TF/Crossplane)
```

### 7.6 Training corpus for IDP

```
- backstage/backstage source + ALL community plugins (roadie/* / spotify/*)
- score-spec/spec + reference implementations (score-compose/score-helm/score-k8s)
- Humanitec docs + Resource Definition examples
- Port templates marketplace
- Cortex YAML scorecard library
- platformengineering.org community articles
- KubeCon Platform Engineering Day talks (transcripts)
```

---

## 8. Edge + Serverless Platforms

### 8.1 Latency / cold-start reality (2025)

| Platform | P50 latency | Cold start | POPs |
|----------|------------|-----------|------|
| Cloudflare Workers | 10–30ms | <1ms (V8 isolates) | 330+ |
| Vercel Edge Functions | <50ms | sub-50ms | 18 (uses Lambda@Edge under hood in some regions) |
| Lambda@Edge (Node) | 50–80ms | 250–800ms | AWS edge POPs |
| Lambda@Edge (Python) | similar | 400–1200ms | same |
| Fastly Compute@Edge (WASM) | ~5–10ms | <1ms | 80+ |
| Deno Deploy | low | low | global |
| Bun runtime | fastest cold-start of any Node-compat | n/a | self-hosted |

Cloudflare Workers ~441% faster than Lambda at p95, and unlimited bandwidth on free tier.

### 8.2 Cloudflare ecosystem

```
Workers       → V8 isolate functions (JS/TS/WASM)
Pages         → static + Workers (serverless full-stack)
R2            → S3-compatible object storage, zero egress
D1            → serverless SQLite (replicated)
KV            → eventually-consistent KV
Durable Objects → strongly-consistent stateful primitives
Queues        → managed message queue
Workers AI    → run LLMs at the edge (Llama, Whisper, Stable Diffusion)
Vectorize     → vector DB (RAG at edge)
Hyperdrive    → connection pooler for Postgres/MySQL behind edge
Stream        → video transcoding + delivery
```

### 8.3 Vercel ecosystem

```
Edge Functions    → Cloudflare Workers-compatible runtime (Node + Python + Go + Ruby)
Edge Middleware   → run BEFORE the request enters serverless
Serverless Funcs  → Lambda@Edge under the hood
Postgres         → managed Postgres (built on Neon)
KV               → built on Upstash Redis
Blob             → object storage
```

### 8.4 Multi-region edge strategies

```
Pattern 1 — Edge cache + origin in primary region
  Cloudflare cache → S3/Lambda in us-east-1
  Trade: simple, 100ms+ for cache misses

Pattern 2 — Workers + DB-at-edge
  CF Workers → D1/Hyperdrive
  Trade: edge writes; eventual consistency
  Use: read-heavy auth, profile, feature flags

Pattern 3 — Multi-region active/active
  CF LB → Workers in EU + US + APAC → regional Aurora DSQL
  Trade: cost 2x; near-zero RTO across regions

Pattern 4 — Global table + edge CDN
  CF Cache → Lambda → DynamoDB Global Tables (multi-master)
  Trade: replication lag; eventual consistency
```

### 8.5 WASM serverless (2025–2026)

- WASI 0.2 (Component Model) GA → portable across runtimes (Wasmtime, Spin, wasmCloud, Wasmer).
- Cold starts: microseconds (vs 100–500ms for containers).
- Major clouds now offer Wasm-based FaaS as mainstream option.
- Wing language **shutdown April 2025** — OSS code lives on but no commercial backing.

---

## 9. Database Platform

### 9.1 Postgres options

| Service | Multi-region | Best for |
|---------|--------------|----------|
| RDS Postgres | Read replicas | Standard managed |
| Aurora Postgres | Cross-region read replicas + Global DB (1 writer) | Standard scale-out |
| **Aurora DSQL** | **Active/active, strong consistency, GA May 2025** | **New globally-distributed apps** |
| AlloyDB (GCP) | HA + read pool nodes | Postgres-compat OLTP+OLAP at GCP |
| Cloud SQL (GCP) | Single-region HA | Standard managed |
| Azure Database for Postgres Flex | Single-region HA | Standard managed |
| Neon | Branching (Git-like) | Dev velocity |
| Supabase | Postgres + auth + realtime | Full BaaS |
| Crunchy Bridge | Multi-cloud Postgres | Vendor-neutral |
| PlanetScale | (Now Postgres + Vitess) | Sharded scale-out |

### 9.2 Aurora DSQL deep cuts (GA May 2025)

- Disaggregated architecture: query processor + adjudicator + journal + crossbar — each scales independently.
- 99.99% single-region SLA, 99.999% multi-region.
- Active/active multi-master (peers); third region as log-only witness.
- Region groupings only — US (us-east-1, us-east-2, us-west-2), EU (eu-west-1/2/3), APAC (ap-northeast-1/2/3).
- No cross-continent yet.
- PostgreSQL wire-compatible.

### 9.3 Distributed SQL (NewSQL)

| DB | TPC-C (TPS) | PG compat | Multi-region |
|----|-------------|-----------|--------------|
| CockroachDB | 45k | wire only | Best with geo-partitioning |
| YugabyteDB | 48k | full (reuses PG query layer) | Strong with row-level geo |
| TiDB | 40k+ (write-heavy lead) | MySQL primary | ✓ |
| Aurora DSQL | benchmarked fastest by AWS | wire | Region-grouped |
| Spanner | 1M+ at scale | GoogleSQL or PG dialect | Global by design |

YugabyteDB wins for PG migration (full compat). CockroachDB wins for geo-partitioning. Spanner remains gold standard at hyperscale.

### 9.4 Vitess (MySQL sharding)

- Open-source MySQL sharding system.
- Powers YouTube, Slack, GitHub, PlanetScale.
- Functions: query routing, online schema migration (with `gh-ost`), connection pooling, transparent sharding.
- Newer alternative: CockroachDB / Aurora DSQL eliminate manual sharding.

### 9.5 NoSQL

```
DynamoDB             → AWS, single-digit-ms; on-demand or provisioned
DynamoDB Global Tables → multi-region multi-master (last-writer-wins)
Spanner              → strongly consistent global
Cosmos DB            → multi-model (SQL/MongoDB/Cassandra/Gremlin); 5 consistency levels
Cassandra/Scylla     → wide-column; high write throughput
MongoDB Atlas        → document; managed across all 3 clouds
```

### 9.6 Vector DBs (2025 production benchmarks)

| DB | p99 latency | QPS | Notable |
|----|-------------|-----|---------|
| Qdrant | 2ms | 12k | Best low-latency, $25/mo+ cloud |
| Milvus / Zilliz | 5ms | 8k | Billion-scale; built-in BM25 + dense (30x faster than Elasticsearch) |
| Pinecone | 8ms | 5k | Fully managed, 99% recall |
| Weaviate | 10ms | 4k | BlockMax WAND (10x keyword speed); MUVERA multi-vector |
| pgvector | varies | depends | If you already have Postgres |
| OpenSearch k-NN | varies | depends | If you already have OpenSearch |

### 9.7 Migration tools (2025)

| Tool | Approach | Best for |
|------|----------|----------|
| Liquibase | Imperative changelogs (XML/YAML/JSON/SQL); FSL license post-v5; AI rollback assist (2025) | Multi-DB enterprise |
| Flyway | Numbered SQL files; Java ecosystem standard; Teams tier discontinued 2025 | Java teams |
| Atlas (atlasgo.io) | Declarative HCL + computed migration plan | Terraform-style schema-as-code |
| Prisma Migrate | Declarative, ORM-coupled | Node/TS apps |
| goose | Plain SQL/Go migrations | Go services |

Atlas is the modern recommendation — same paradigm as Terraform.

---

## 10. Networking Deep

### 10.1 DNS

```
Route53          → AWS native, latency/geo/failover; alias records to AWS resources
Cloud DNS        → GCP native
Azure DNS        → Azure native
Cloudflare DNS   → fastest authoritative (1.1.1.1 is recursive); free
NS1 / Constellix → enterprise multi-cloud DNS, advanced traffic steering
```

### 10.2 CDN performance (Cloudflare 95p TTFB benchmark, Nov 2024–Mar 2025)

- Cloudflare fastest in ~48% of top 1000 networks.
- Fastly extremely close in many networks (e.g., +0.2% lead on Comcast).
- CloudFront strong inside AWS-heavy stacks (free egress to AWS origins).
- All have edge compute now: Workers / Compute@Edge / Lambda@Edge.

### 10.3 Load balancers (AWS)

```
ALB  (L7)  → HTTP/HTTPS/gRPC; WAF integration; target group flexibility
NLB  (L4)  → TCP/TLS/UDP; static IPs; >millions RPS
GWLB        → traffic inspection (third-party firewall in chain)
ELB Classic → legacy, avoid
GAL (Global Accelerator) → anycast IPs in front of ALB/NLB for global traffic
```

### 10.4 Zero Trust Network Access (2025)

| Tool | Architecture | Best fit |
|------|-------------|----------|
| Tailscale | WireGuard mesh + identity overlay | Fastest dev access; great for SSH/RDP/DB |
| Twingate | Layer 4 ZTNA (no mesh); resource-grain | App-name + group-based access |
| Cloudflare Access + WARP | SASE — Access for apps + Gateway for SWG | When Cloudflare is the wider stack |
| Zscaler | Enterprise SASE | Big-org compliance |
| Pomerium | Self-hosted reverse-proxy ZTNA | OSS option |

Tailscale wins on dev velocity (sign in, get tailnet); Cloudflare Access wins on full SASE; Twingate wins on resource granularity.

### 10.5 WAF

```
AWS WAF              → tied to CloudFront/ALB/API Gateway
Cloudflare WAF       → in front of any origin
Azure Front Door WAF → tied to AFD
Akamai App & API Protector → enterprise
```

### 10.6 DDoS protection

```
AWS Shield Advanced  → $3000/mo + transfer; 24/7 SRT
Cloudflare           → unmetered DDoS protection (free tier!)
Google Cloud Armor   → tier-based
Azure DDoS Protection Standard → per-resource
```

---

## 11. FinOps + Cost Engineering

### 11.1 FinOps Foundation Framework 2025 (Inform / Optimize / Operate)

```
INFORM   → Visibility, allocation, benchmarking, budgeting, forecasting
OPTIMIZE → Identify and execute waste reduction
OPERATE  → KPI tracking, governance policies aligned with business
```

### 11.2 2025 framework changes — **Scopes**

The 2025 Framework adds **Scopes** as a structural element. Scopes define context: Public Cloud, SaaS (Snowflake, Salesforce), GenAI (LLM API spend), Data Center, Private Cloud. Each capability is now applied **per scope**.

### 11.3 Cost allocation tags (mandatory at provision time)

```
Required tags for every resource:
- Environment  : prod/staging/dev/sandbox
- Owner        : team-name (matches catalog)
- CostCenter   : finance code
- Project      : product/feature
- DataClass    : public/internal/confidential/regulated
```

Enforce via:
- AWS: SCP `aws:RequestTag/X` (deny on creation), Tag Policies
- GCP: Org Policy required labels
- Azure: Azure Policy required tags

### 11.4 Showback / chargeback

```
Showback   → "your team used $X" (no actual billing)
Chargeback → cross-charge cost center (real finance impact)
```

Tools: Vantage, CloudHealth, Apptio Cloudability, Kubecost (k8s-specific), Infracost (pre-deploy IaC estimate).

### 11.5 Anomaly detection

```
AWS Cost Anomaly Detection (free)
Vantage anomalies + alerts (commercial)
CloudZero / Spend.io
ProsperOps                → automated commitment management
```

### 11.6 Right-sizing automation

```
AWS Compute Optimizer    → free; recs for EC2, Lambda, EBS, ASG, ECS
GCP Recommender          → equivalent
Azure Advisor            → equivalent
ScaleOps / StormForge    → K8s VPA recommender for prod
```

### 11.7 Spot orchestration (Karpenter, ProsperOps)

Already covered §6.9. Karpenter native AWS spot fleet; CAST AI / ScaleOps for cross-cloud.

### 11.8 Training corpus for FinOps

```
- FinOps Foundation framework docs (finops.org/framework)
- AWS / GCP / Azure cost optimization whitepapers
- Vantage / CloudZero / Apptio public benchmarks
- KubeCon FinOps track talks (transcripts)
- Real customer cost-cut case studies (already collected: Tinybird, Series B SaaS examples)
```

---

## 12. 2025–2026 Platform Engineering Trends

### 12.1 Internal LLM gateways (the 2026 must-have)

| Tool | Type | Key strength | Cost |
|------|------|-------------|------|
| **LiteLLM** | OSS, self-host | OpenAI-compat; cheapest at $10k+ MRR; 100+ providers | Free + infra |
| **Portkey** | SaaS or self-host | SOC2/HIPAA/ISO27001; observability; 250+ LLMs | $49/mo+ |
| **OpenRouter** | SaaS | Pay-per-token; consumer-friendly | 5% markup |
| **Helicone** | OSS observability | Caching + analytics | Free + cloud |
| **Truefoundry / Bifrost** | SaaS | LLM gateway + ML platform | Quote |

LiteLLM is the **default for orgs serious about cost** — runs as your own proxy, no markup.

### 12.2 AI agents in platform engineering

- **Resolve.ai** — AI SRE; auto-investigates alerts, RCA in minutes, MTTR -80%; customers: Coinbase, DoorDash, Toast, Zscaler. $40M Series A extension at $1.5B (2026).
- **Aviator (aviator.co)** — AI code review + merge queues + deployment.
- **OpenText DevOps Aviator** — AI for performance engineering scripts.
- **Cursor / Sourcegraph Cody / GitHub Copilot Workspace** — IDE-side coding agents.
- **Codeium / Tabnine / Continue** — open-source IDE agents.

### 12.3 Per-PR ephemeral environments

| Tool | Approach |
|------|----------|
| Coherence | PR comment with auto-preview URL; spot-backed for cost |
| Uffizzi | OSS + cloud; vCluster-based isolated environments |
| Render Preview | Built-in to Render |
| Vercel Preview | Built-in to Vercel |
| Netlify Deploy Previews | Built-in to Netlify |
| Argo CD ApplicationSet PR Generator | OSS K8s-native |
| vCluster + ArgoCD | DIY pattern; cheapest at scale |

Best practice: every PR gets a unique URL, smoke tests run against it, design reviewer can click before merge.

### 12.4 WASM-based services

- Production runtimes: Wasmtime (Bytecode Alliance), wasmCloud, Spin (Fermyon), Wasmer.
- Use cases: edge serverless, plugin systems (Envoy filters, Istio extensions, Postgres extensions), embedded scripting.
- Platforms moving to Wasm: Fastly Compute@Edge (WASM-only), Cloudflare Workers (V8 + WASM), Spin Hub.

### 12.5 AI-native databases / observability

```
LangSmith        → LLM tracing + evals (LangChain)
Helicone         → LLM tracing + caching (OSS option)
Phoenix (Arize)  → OSS LLM observability
Langfuse         → OSS, self-host LLM observability
Weights & Biases Weave → MLOps + LLM
```

### 12.6 Autonomous Cloud Engineer (the Surrogate-1 mission)

The path is converging on:

1. **MCP** (Model Context Protocol) — standardize how agents pull cloud state. AWS docs MCP, OpenCost MCP (2025), terraform-mcp-server.
2. **Multi-agent systems** — research / planner / executor / critic agents (CrewAI, LangGraph, AutoGen).
3. **Tool-using agents** — agents that call `terraform plan`, `kubectl apply`, `aws sts get-caller-identity`, `gh pr create`.

Surrogate-1's training MUST include MCP-call patterns + tool-use traces.

---

## 13. Training Data Sources

### 13.1 Curated GitHub repos

```
Cloud
- awesome-aws (donnemartin)
- awesome-gcp (GoogleCloudPlatform/awesome-google-cloud)
- awesome-azure (kristofferandreasen/awesome-azure)
- aws-samples/* (8000+ official AWS samples)
- GoogleCloudPlatform/* (1500+ GCP samples)
- Azure-Samples/*

K8s
- run-x/awesome-kubernetes
- ramitsurana/awesome-kubernetes
- tomhuang12/awesome-k8s-resources
- kubernetes/kubernetes (source + KEPs)
- kubernetes-sigs/* (CAPI, Gateway API, Karpenter)
- helm/charts (deprecated but reference)
- bitnami/charts
- argoproj/argo-cd

IaC
- hashicorp/terraform
- terraform-aws-modules/* (40+ official modules)
- terraform-google-modules/*
- Azure/terraform-azurerm-* (AVM)
- pulumi/examples
- aws/aws-cdk
- crossplane/crossplane + upbound/configurations

Platform
- backstage/backstage + roadie/* + spotify/* community plugins
- score-spec/spec
- humanitec-architecture/*

Eval
- codefuse-ai/codefuse-devops-eval
- IaC-Eval (academic)
- NL2Bash
```

### 13.2 Reddit communities (curate top-voted threads, last 2 yrs)

- r/devops, r/aws, r/AZURE, r/googlecloud, r/kubernetes, r/Terraform, r/sysadmin, r/sre, r/platformengineering

### 13.3 Conference talks (transcribe via Whisper, MIT-licensed for TLP/CNCF)

- KubeCon + CloudNativeCon (CNCF YouTube; ~600 talks/year)
- AWS re:Invent (multiple thousand sessions, breakouts archived)
- Google Cloud Next (annual)
- Microsoft Ignite / Build
- HashiConf
- PlatformCon (annual, online)
- SREcon (USENIX)

### 13.4 Public datasets on HuggingFace

```
- CatOwl/Terraform                   (Terraform code corpus)
- nvidia/OpenCodeReasoning            (reasoning over code)
- bigcode/the-stack-v2                (filtered code, has IaC files)
- mhhmm/codealpaca-iac                (instruction tuning for IaC)
- Custom: collect from terraform-aws-modules/eks/aws + variants
```

### 13.5 Documentation (for retrieval / SFT context)

- AWS docs (full), GCP docs, Azure docs (Microsoft Learn), CNCF docs, K8s docs, Helm docs, Terraform/OpenTofu docs.
- AWS Well-Architected Framework PDFs (one per pillar).
- Google Cloud Architecture Framework.
- Azure Cloud Adoption Framework + Well-Architected Framework.

### 13.6 Synthesized data (recommended approach)

For Surrogate-1 v2:

```
1. Take each terraform-aws-modules example
2. Mutate: change region, instance type, AZ count, subnet sizes
3. Build instruction format: "Build me a 3-AZ VPC in us-west-2 with public+private+db subnets using terraform-aws-modules/vpc/aws"
4. Output: working main.tf + outputs.tf + variables.tf

5. For each AWS service, generate:
   - "What is X" Q&A from official docs
   - "Compare X vs Y" from official docs
   - "Migrate from X to Y" code examples

6. Multi-step trajectories:
   - "Build me a SaaS platform on AWS" → 30+ step reasoning trace through architecture decisions
```

Total target: ~100k–250k cloud/platform instruction-tuning examples.

---

## 14. Eval Benchmarks

### 14.1 Existing benchmarks

| Benchmark | What it tests | Surrogate-1 fit |
|-----------|---------------|-----------------|
| codefuse-ai/codefuse-devops-eval | DevOps Q&A multiple-choice | Quick sanity check |
| IaC-Eval (academic) | Terraform generation correctness | Direct fit |
| KubeBench (community) | K8s manifest validity | Direct fit |
| NL2Bash | Bash command from NL | Tooling sub-skill |
| BIG-Bench (subset) | Various reasoning | General |
| HumanEval / MBPP | General coding | Already passes (Qwen2.5-Coder-7B baseline) |

### 14.2 Custom Surrogate-1 v2 evals (we author)

```
Surrogate-1 Cloud Eval v2:
1. Terraform generation (200 prompts, varying complexity)
   - Pass = `terraform validate` + `terraform plan` succeeds
   - Score: % passing × % correct logical structure (judge LLM)

2. Helm chart authoring (50 prompts)
   - Pass = `helm template` produces valid YAML
   - Score: % passing × `kubeval` validation rate

3. CDK/CFN authoring (100 prompts)
   - Pass = `cdk synth` succeeds
   - Score: + `cfn-lint` clean rate, + `cfn-guard` policy pass

4. ArgoCD Application + Kustomize (50 prompts)
   - Pass = ArgoCD CLI dry-run succeeds

5. Multi-cloud DR scenario (30 prompts)
   - Open-ended: "Design active/passive across AWS+GCP for a SaaS, RTO=15min, RPO=1min"
   - Score: judged by GPT-5 / Claude / human reviewer on architecture quality

6. Cost optimization (50 prompts)
   - Given a `terraform plan` output, return cost reductions (Graviton swap, RIs/SPs, Spot)
   - Score: judged on $$ accuracy (vs Infracost ground truth)

7. K8s troubleshooting (50 prompts)
   - Given pod logs + describe output, return root cause + fix
   - Score: % matching ground truth

8. Tool-use traces (100 prompts)
   - Given a goal, agent must call `aws cli` / `kubectl` / `terraform` correctly
   - Score: % achieving goal (sandbox eval)
```

Total: ~630 prompts. Run with rubric judges (GPT-5/Claude). Surrogate-1 v2 target: **65% overall** (above Qwen2.5-Coder-7B baseline of ~38%).

### 14.3 Capability tiers (target)

| Tier | Capability | v2 Target |
|------|-----------|-----------|
| 1 | Recognize + classify cloud services | 95% |
| 2 | Author single-file IaC (Terraform/CDK/Helm) | 75% |
| 3 | Author multi-file project (VPC + EKS + RDS + ArgoCD) | 60% |
| 4 | End-to-end design trace ("build SaaS on AWS") | 50% |
| 5 | Multi-cloud DR design + tool execution | 35% (stretch) |

---

## v2 Curriculum Integration Plan

For the v2 LoRA fine-tune of Qwen2.5-Coder-7B → Surrogate-1:

### Data mix (target ~250k instruction examples)

```
40%  IaC generation (Terraform / OpenTofu / CDK / Pulumi / Bicep / Crossplane)
20%  K8s authoring (Helm / Kustomize / ArgoCD / Karpenter)
15%  Cloud architecture Q&A (mined from cert prep + docs)
10%  Cost optimization scenarios (FinOps mined + synthesized)
10%  IDP / Backstage / Score / Humanitec patterns
5%   Multi-step tool-use traces (terraform plan → fix → apply)
```

### Key sources (direct ingestion priorities)

```
1. terraform-aws-modules/* + terraform-google-modules/* + Azure AVM (canonical IaC)
2. backstage/backstage source + plugin examples
3. AWS Well-Architected docs (all pillars + lenses)
4. GCP Cloud Adoption Framework
5. CNCF KubeCon transcripts (Whisper-extracted)
6. score-spec + humanitec docs
7. OpenCost docs + MCP-pattern examples
8. Real customer post-mortems (Tinybird $-20k, Series-B SaaS $-29k)
9. IaC-Eval benchmark training set
10. CodeFuse DevOps-Eval training set
```

### Eval gates

- v2 cannot ship until ≥65% overall on Surrogate-1 Cloud Eval v2.
- Tier-3 (multi-file) ≥60% is the practical bar for autonomous infra building.
- Add MCP-tool-use trajectory eval (sandbox terraform/kubectl/aws calls).

---

## Sources Consulted

- AWS Well-Architected Framework (6 pillars docs, Sustainability pillar Nov 2024 refresh)
- Terraform / OpenTofu best practices (Terramate, Spacelift, env0, Scalr 2025 articles)
- Kubernetes 1.32-1.35 release notes; CNCF security blog Dec 2025
- Backstage docs + Spotify Backstage portal blog (2025)
- ArgoCD / FluxCD comparison articles (2025-2026 post-Weaveworks closure)
- Crossplane v2.0 release blog + InfoQ article (Aug 2025)
- Karpenter cost optimization blogs (Tinybird; Series-B SaaS case studies)
- Cloudflare Workers / Vercel Edge / Lambda@Edge benchmarks (2025)
- FinOps Foundation 2025 framework + Scopes update
- Istio / Linkerd / Cilium 2025 benchmarks (deepness-lab academic paper)
- Pulumi / Terraform / CDK / Bicep 2025 comparisons
- CockroachDB / YugabyteDB / Spanner / Aurora DSQL 2025 benchmarks
- AWS SAP-C02 / GCP PCA (Oct 2025 refresh) / Azure AZ-305 (April 2026 refresh)
- Backstage / Port / Cortex / Humanitec IDP comparison (2025-2026)
- Karmada v1.15 + KubeFed EOL + Cluster API
- Coherence / Uffizzi ephemeral environments (2025)
- AWS CDK best practices (CDK Refactor Sept 2025)
- VPC Transit Gateway / PrivateLink hub-spoke patterns
- Helm / Kustomize / Carvel comparison (Helm 4 Nov 2025)
- terraform-aws-modules registry top downloads (May 2025 stats)
- Liquibase / Flyway / Atlas migration tools (2025 license + features)
- Aurora DSQL GA announcement (May 2025)
- CDN benchmarks (Cloudflare 95p TTFB 2024-2025)
- AWS Savings Plans / Reserved Instances June 2025 policy changes
- IAM SCPs + Permission Boundaries + ABAC patterns
- GKE / EKS / AKS managed K8s comparison (2025-2026)
- terraform-aws-modules registry usage (vpc 126M, eks 96.3M downloads)
- Vertex AI / BigQuery / Gemini integration (2025)
- Resolve.ai AI SRE + Aviator (2025-2026)
- LiteLLM / Portkey / OpenRouter LLM gateway comparison (2025)
- Multi-cloud DR active/active vs active/passive patterns
- Wing language shutdown (April 2025) + WASM serverless trends
- Awesome-aws / awesome-kubernetes curated lists
- Kubecost / OpenCost cost visibility (Kubecost IBM acquisition 2024)
- Atlantis / Spacelift / Env0 / Terramate IaC platforms
- Score spec + OAM workload specifications
- Karpenter NodePool + Spot + Graviton best practices
- Tailscale / Twingate / Cloudflare Access ZTNA comparison
- Vector DB benchmarks (Pinecone / Weaviate / Qdrant / Milvus 2025)
- AWS Copilot end-of-support (June 12 2026) + SAM + Amplify
- Gateway API + ingress-nginx retirement (March 2026)
- DevOps eval benchmarks + IaC-Eval academic benchmark
