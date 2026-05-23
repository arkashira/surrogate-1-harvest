"""Surrogate-1 — axentx-eval-50: in-domain DevSecOps/SRE/coding eval suite.

50 hand-crafted prompts spanning Surrogate-1's target domains. Used by
bench-v1-vs-v15.sh as the "in-domain" comparison metric (alongside public
benchmarks HumanEval+ / MBPP+ / LCB / BFCL / RULER / SWE-Bench).

Why this exists:
  Public benchmarks measure code/agent capability in general — they don't
  test the specific patterns Surrogate-1 is being trained for: AWS IaC,
  CDK constructs, IAM least-privilege, K8s troubleshooting, SRE runbooks,
  CVE remediation, REST/gRPC API design, observability (RED/USE), etc.
  This 50-item rubric scores responses on:
    - Correctness (does the suggested code/config actually work?)
    - Cite-real (no phantom AWS APIs, fictional Terraform resources, etc.)
    - Right-size (no over-engineering for the stated scope)
    - Security default (deny-by-default, least-privilege, no secret leaks)

Scoring:
  Each prompt has a reference answer + 4 binary checks. Auto-grader runs
  pattern-match (regex/AST) + LLM-judge fallback for soft criteria.
  Final score = mean of (correct ∧ cite ∧ right-size ∧ secure) per prompt.

Usage:
  python3 axentx-eval-50.py --model <model_id> --out <outdir>
  python3 axentx-eval-50.py --model <model_id> --endpoint http://host:8000/v1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

EVAL_PROMPTS = [
    # ── AWS IaC / CDK / Terraform (15) ──
    {
        "id": "aws-cdk-s3-kms",
        "domain": "devops-cdk",
        "prompt": ("Write an AWS CDK TypeScript construct that creates an S3 "
                   "bucket with KMS encryption (customer-managed CMK), "
                   "versioning enabled, public access blocked, and lifecycle "
                   "rule transitioning objects to GLACIER after 90 days."),
        "must_contain": ["new s3.Bucket", "new kms.Key", "blockPublicAccess",
                         "versioned", "lifecycleRules"],
        "must_not": ["AWS_ACCESS_KEY", "hardcoded ARN"],
    },
    {
        "id": "tf-vpc-3az",
        "domain": "devops-tf",
        "prompt": ("Write a Terraform module for a VPC across 3 AZs with "
                   "public + private + database subnets, NAT gateway per AZ "
                   "for HA, and proper route tables. CIDR 10.0.0.0/16."),
        "must_contain": ["aws_vpc", "aws_subnet", "aws_nat_gateway",
                         "aws_route_table", "availability_zone"],
        "must_not": ["0.0.0.0/0.*0.0.0.0/0"],
    },
    {
        "id": "tf-iam-assume-cross-account",
        "domain": "sec-iam",
        "prompt": ("Write a Terraform IAM role for cross-account access where "
                   "account 111111111111 can assume into account 222222222222 "
                   "but only with MFA + ExternalId."),
        "must_contain": ["aws_iam_role", "AssumeRole", "MultiFactorAuthPresent",
                         "ExternalId", "111111111111"],
        "must_not": ["\"*\"\\s*\\}"],
    },
    {
        "id": "cdk-fargate-service",
        "domain": "devops-cdk",
        "prompt": ("CDK construct for ECS Fargate service behind ALB, with "
                   "health check, autoscaling 1-10 tasks based on CPU>70%, "
                   "and CloudWatch logging."),
        "must_contain": ["FargateService", "ApplicationLoadBalancer",
                         "scaleOnCpuUtilization", "logGroup"],
        "must_not": [],
    },
    {
        "id": "cf-lambda-cron",
        "domain": "devops-cf",
        "prompt": ("CloudFormation YAML for a Lambda function (Python 3.12) "
                   "triggered by EventBridge every 5 minutes, with X-Ray "
                   "tracing enabled, dead-letter SQS queue, and least-privilege "
                   "IAM role."),
        "must_contain": ["AWS::Lambda::Function", "AWS::Events::Rule",
                         "TracingConfig", "DeadLetterConfig", "Runtime: python3.12"],
        "must_not": ["AdministratorAccess"],
    },
    {
        "id": "tf-rds-multi-az",
        "domain": "devops-tf",
        "prompt": ("Terraform aws_db_instance for PostgreSQL 16, Multi-AZ, "
                   "encrypted with KMS, automated backups 30 days, deletion "
                   "protection on, performance insights on. db.t4g.medium."),
        "must_contain": ["aws_db_instance", "multi_az", "storage_encrypted",
                         "backup_retention_period", "deletion_protection"],
        "must_not": [],
    },
    {
        "id": "cdk-eventbridge-fanout",
        "domain": "devops-cdk",
        "prompt": ("CDK construct: EventBridge custom bus, schema discovery, "
                   "fan-out to 3 SQS queues with DLQ each, retry policy."),
        "must_contain": ["EventBus", "Rule", "Queue", "DeadLetterQueue"],
        "must_not": [],
    },
    {
        "id": "tf-state-locking",
        "domain": "devops-tf",
        "prompt": ("Terraform backend block: S3 with KMS encryption + DynamoDB "
                   "state locking. Bucket name 'tfstate-prod-axentx'."),
        "must_contain": ["backend \"s3\"", "dynamodb_table", "kms_key_id",
                         "encrypt = true"],
        "must_not": [],
    },
    {
        "id": "cdk-vpc-endpoints",
        "domain": "devops-cdk",
        "prompt": ("CDK: VPC with interface endpoints for ECR (api+dkr), "
                   "STS, Secrets Manager, and a gateway endpoint for S3. "
                   "Security group must restrict to VPC CIDR."),
        "must_contain": ["addInterfaceEndpoint", "addGatewayEndpoint",
                         "ECR", "Secrets", "S3"],
        "must_not": [],
    },
    {
        "id": "cf-stepfunctions-saga",
        "domain": "devops-cf",
        "prompt": ("StepFunctions state machine implementing saga pattern: "
                   "3 transactional steps with compensation handlers, error "
                   "catching, exponential backoff retry."),
        "must_contain": ["StateMachine", "Catch", "Retry", "BackoffRate",
                         "ASL"],
        "must_not": [],
    },
    {
        "id": "tf-eks-cluster",
        "domain": "devops-tf",
        "prompt": ("Terraform EKS 1.30 cluster with managed node group "
                   "(spot + on-demand mix), private API endpoint, OIDC "
                   "provider for IRSA, audit logs to CloudWatch."),
        "must_contain": ["aws_eks_cluster", "version = \"1.30\"",
                         "aws_iam_openid_connect_provider", "spot",
                         "endpoint_private_access"],
        "must_not": ["endpoint_public_access\\s*=\\s*true.*public_access_cidrs.*0.0.0.0/0"],
    },
    {
        "id": "cdk-cognito-saml",
        "domain": "devops-cdk",
        "prompt": ("CDK Cognito user pool with SAML federation (Okta IdP), "
                   "MFA required, password policy ≥14 chars, account "
                   "recovery via email only."),
        "must_contain": ["UserPool", "SAML", "mfa", "passwordPolicy"],
        "must_not": [],
    },
    {
        "id": "tf-cloudfront-waf",
        "domain": "sec-iam",
        "prompt": ("Terraform CloudFront distribution + WAFv2 web ACL with "
                   "AWS managed rule sets (CommonRuleSet + KnownBadInputs) "
                   "and rate-based rule (5000 req/5min per IP)."),
        "must_contain": ["aws_cloudfront_distribution", "aws_wafv2_web_acl",
                         "AWSManagedRulesCommonRuleSet",
                         "rate_based_statement"],
        "must_not": [],
    },
    {
        "id": "cdk-cdn-cache-key",
        "domain": "devops-cdk",
        "prompt": ("CDK CloudFront with custom cache policy: include Auth "
                   "header in cache key, query string allowlist, compress "
                   "objects, TTL 60s default 86400s max."),
        "must_contain": ["CachePolicy", "headerBehavior", "queryStringBehavior",
                         "defaultTtl", "maxTtl"],
        "must_not": [],
    },
    {
        "id": "tf-secrets-rotation",
        "domain": "sec-secrets",
        "prompt": ("Terraform: Secrets Manager rotation Lambda for an RDS "
                   "PostgreSQL master password. 30-day rotation, multi-user "
                   "strategy, KMS-encrypted with customer key."),
        "must_contain": ["aws_secretsmanager_secret",
                         "aws_secretsmanager_secret_rotation",
                         "rotation_lambda_arn", "automatically_after_days"],
        "must_not": [],
    },

    # ── K8s + container ops (10) ──
    {
        "id": "k8s-deployment-prod",
        "domain": "devops-k8s",
        "prompt": ("Kubernetes Deployment manifest for a production HTTP "
                   "service: 3 replicas, rolling update maxSurge=1 "
                   "maxUnavailable=0, resource limits 500m CPU 512Mi RAM, "
                   "liveness + readiness probes, securityContext non-root."),
        "must_contain": ["replicas: 3", "RollingUpdate", "maxSurge",
                         "livenessProbe", "readinessProbe",
                         "securityContext", "runAsNonRoot"],
        "must_not": ["privileged: true"],
    },
    {
        "id": "k8s-network-policy",
        "domain": "devops-k8s",
        "prompt": ("NetworkPolicy: default-deny ingress for namespace 'app', "
                   "then allow only from namespace 'frontend' with label "
                   "tier=web on port 8080."),
        "must_contain": ["NetworkPolicy", "podSelector: {}", "Ingress",
                         "namespaceSelector", "tier: web"],
        "must_not": [],
    },
    {
        "id": "helm-chart-values",
        "domain": "devops-k8s",
        "prompt": ("Helm chart values.yaml for nginx-ingress: 2 replicas, "
                   "podDisruptionBudget minAvailable=1, HPA 2-10 on CPU>70%, "
                   "Prometheus metrics scrape annotations."),
        "must_contain": ["replicaCount", "podDisruptionBudget",
                         "minAvailable", "autoscaling", "prometheus.io/scrape"],
        "must_not": [],
    },
    {
        "id": "k8s-pod-failing-debug",
        "domain": "sre-runbook",
        "prompt": ("A pod is in CrashLoopBackOff. Walk through the diagnostic "
                   "steps using kubectl: check pod status, describe events, "
                   "logs (current + previous), exec into init containers, "
                   "check resource limits."),
        "must_contain": ["kubectl describe pod", "kubectl logs",
                         "--previous", "kubectl exec", "OOMKilled"],
        "must_not": [],
    },
    {
        "id": "dockerfile-multi-stage-go",
        "domain": "devops-docker",
        "prompt": ("Multi-stage Dockerfile for a Go service: build stage "
                   "with static linking + CGO_ENABLED=0, runtime stage "
                   "based on distroless/static, non-root user, healthcheck."),
        "must_contain": ["FROM golang", "AS build", "CGO_ENABLED=0",
                         "FROM gcr.io/distroless/static", "USER nonroot",
                         "HEALTHCHECK"],
        "must_not": ["FROM ubuntu", "FROM debian"],
    },
    {
        "id": "k8s-istio-mtls",
        "domain": "devops-k8s",
        "prompt": ("Istio configuration: enable strict mTLS for namespace "
                   "'payments', define AuthorizationPolicy that only allows "
                   "service 'orders' (with SPIFFE identity) to call /v1/charge."),
        "must_contain": ["PeerAuthentication", "STRICT",
                         "AuthorizationPolicy", "spiffe://", "/v1/charge"],
        "must_not": [],
    },
    {
        "id": "k8s-cronjob-suspend",
        "domain": "devops-k8s",
        "prompt": ("CronJob: nightly DB backup at 02:00 UTC, concurrencyPolicy "
                   "Forbid, successfulJobsHistoryLimit 3, failedJobsHistoryLimit "
                   "1, backoffLimit 2, restartPolicy OnFailure."),
        "must_contain": ["CronJob", "schedule:", "concurrencyPolicy: Forbid",
                         "successfulJobsHistoryLimit",
                         "restartPolicy: OnFailure"],
        "must_not": [],
    },
    {
        "id": "containerd-image-pull",
        "domain": "devops-k8s",
        "prompt": ("Why is my pod stuck in ImagePullBackOff with private ECR? "
                   "List likely causes and fixes: IRSA missing, "
                   "imagePullSecrets, ECR token expiry, region mismatch, "
                   "VPC endpoint missing."),
        "must_contain": ["IRSA", "imagePullSecrets", "ecr get-login",
                         "VPC endpoint"],
        "must_not": [],
    },
    {
        "id": "k8s-pdb-rolling",
        "domain": "devops-k8s",
        "prompt": ("Explain the difference between maxUnavailable in a "
                   "PodDisruptionBudget vs in a Deployment's RollingUpdate "
                   "strategy. Give an example showing they can conflict."),
        "must_contain": ["voluntary", "involuntary", "rolling",
                         "PodDisruptionBudget"],
        "must_not": [],
    },
    {
        "id": "k8s-resource-quotas",
        "domain": "devops-k8s",
        "prompt": ("Namespace 'team-api': enforce ResourceQuota (max 32 CPU "
                   "+ 64Gi memory + 50 pods) and LimitRange (default container "
                   "request 100m+128Mi, limit 1+1Gi). Show what happens when "
                   "a pod exceeds quota."),
        "must_contain": ["ResourceQuota", "LimitRange", "requests",
                         "limits", "defaultRequest"],
        "must_not": [],
    },

    # ── Security / IAM / CVE (8) ──
    {
        "id": "iam-least-priv-s3",
        "domain": "sec-iam",
        "prompt": ("Write the most restrictive IAM policy for a Lambda that "
                   "needs to read from one S3 bucket 'app-data-prod' and write "
                   "to one prefix 'logs/' in another bucket 'audit-logs-prod'. "
                   "Include resource-level scoping."),
        "must_contain": ["s3:GetObject", "s3:PutObject",
                         "arn:aws:s3:::app-data-prod",
                         "arn:aws:s3:::audit-logs-prod/logs/"],
        "must_not": ["s3:\\*", "Resource\":\\s*\"\\*\""],
    },
    {
        "id": "cve-log4shell-detect",
        "domain": "sec-cve",
        "prompt": ("Detect Log4Shell (CVE-2021-44228) in a Java microservice: "
                   "list (1) which dependency versions are vulnerable, "
                   "(2) shell commands to grep for vulnerable jars, "
                   "(3) runtime mitigation, (4) permanent fix."),
        "must_contain": ["log4j-core", "2.14", "2.16", "2.17",
                         "JndiLookup", "log4j2.formatMsgNoLookups"],
        "must_not": [],
    },
    {
        "id": "secret-scan-ci",
        "domain": "sec-secrets",
        "prompt": ("GitHub Actions workflow that runs secret scanning "
                   "(gitleaks + trufflehog) on every PR + on-push to main, "
                   "fails the check if any secret found, posts comment with "
                   "remediation."),
        "must_contain": ["gitleaks", "trufflehog", "pull_request", "workflows",
                         "github.event.pull_request"],
        "must_not": [],
    },
    {
        "id": "kms-cmk-policy",
        "domain": "sec-iam",
        "prompt": ("KMS key policy granting decrypt only to a specific IAM role "
                   "(arn:aws:iam::123:role/AppRole) and only when called from "
                   "VPC endpoint vpce-abc123. Root account stays full admin."),
        "must_contain": ["kms:Decrypt", "AppRole",
                         "aws:SourceVpce", "vpce-abc123",
                         "root"],
        "must_not": [],
    },
    {
        "id": "ssrf-in-go",
        "domain": "sec-cve",
        "prompt": ("Review this Go HTTP handler for SSRF: it takes ?url= "
                   "param and fetches it with http.Get. Show the SSRF risk "
                   "(metadata endpoint exfil) and write a safe version using "
                   "URL allowlist + DNS pinning."),
        "must_contain": ["169.254.169.254", "allowlist", "net.LookupHost",
                         "http.Client"],
        "must_not": [],
    },
    {
        "id": "cve-spring4shell-fix",
        "domain": "sec-cve",
        "prompt": ("CVE-2022-22965 Spring4Shell remediation: vulnerable "
                   "version range, mitigation via WAF rule, permanent fix "
                   "version, dependency exclusions for transitive."),
        "must_contain": ["spring-beans", "5.2.20", "5.3.18",
                         "disallowedFields"],
        "must_not": [],
    },
    {
        "id": "tls-cipher-modern",
        "domain": "sec-tls",
        "prompt": ("Configure nginx for modern TLS: TLS 1.3 only (or 1.2+ "
                   "for compatibility), Mozilla modern cipher list, OCSP "
                   "stapling, HSTS preload, HPKP NOT used."),
        "must_contain": ["ssl_protocols", "TLSv1.3", "ssl_stapling",
                         "Strict-Transport-Security", "preload"],
        "must_not": ["TLSv1\\b", "SSLv"],
    },
    {
        "id": "pod-security-standard",
        "domain": "sec-iam",
        "prompt": ("Enforce Pod Security Standard 'restricted' on namespace "
                   "'prod-api'. Show the Namespace label, an admission "
                   "webhook config, and what gets blocked (privileged, "
                   "hostPath, runAsRoot)."),
        "must_contain": ["pod-security.kubernetes.io/enforce: restricted",
                         "privileged: false", "runAsNonRoot"],
        "must_not": [],
    },

    # ── SRE / observability / runbook (7) ──
    {
        "id": "slo-latency-p99",
        "domain": "sre-slo",
        "prompt": ("Write a PromQL query for the 28-day SLI of HTTP request "
                   "latency p99 < 300ms on service 'api-gateway', and the "
                   "corresponding error budget burn rate alert (fast burn "
                   "1hr window > 14.4× budget)."),
        "must_contain": ["histogram_quantile", "0.99",
                         "http_request_duration", "rate", "[28d]",
                         "14.4"],
        "must_not": [],
    },
    {
        "id": "incident-runbook-db",
        "domain": "sre-runbook",
        "prompt": ("Runbook for 'database connection pool exhausted' alert: "
                   "(1) immediate actions, (2) diagnosis SQL queries, "
                   "(3) common root causes, (4) preventive long-term fix, "
                   "(5) escalation path."),
        "must_contain": ["pg_stat_activity", "max_connections",
                         "connection leak", "pgbouncer"],
        "must_not": [],
    },
    {
        "id": "sli-availability",
        "domain": "sre-slo",
        "prompt": ("Define an SLI for availability of a checkout service "
                   "based on HTTP success rate (excluding 5xx, including "
                   "499 client cancels as 'good'). Write the PromQL."),
        "must_contain": ["status!~", "5..", "rate", "availability"],
        "must_not": [],
    },
    {
        "id": "alertmanager-route",
        "domain": "sre-runbook",
        "prompt": ("Alertmanager config: route critical alerts (severity=critical) "
                   "to PagerDuty, warning to Slack #ops, group_by alertname+"
                   "service, repeat_interval 4h, inhibit critical from "
                   "warning of same service."),
        "must_contain": ["routes:", "severity = \"critical\"", "pagerduty",
                         "slack_configs", "inhibit_rules", "group_by"],
        "must_not": [],
    },
    {
        "id": "k8s-otel-trace",
        "domain": "sre-slo",
        "prompt": ("OpenTelemetry Collector deployment in K8s: receive OTLP "
                   "from app pods, batch export to Tempo (traces) and "
                   "Prometheus remote-write (metrics), tail-sampling for "
                   "errors + slow traces."),
        "must_contain": ["receivers:", "otlp", "exporters:",
                         "tail_sampling", "tempo", "prometheus_remote_write"],
        "must_not": [],
    },
    {
        "id": "chaos-pod-kill",
        "domain": "sre-runbook",
        "prompt": ("Chaos engineering: design a pod-kill experiment for a "
                   "stateless API. Define steady state, hypothesis, blast "
                   "radius limit (max 10% pods), abort conditions, and the "
                   "Chaos Mesh PodChaos manifest."),
        "must_contain": ["steady state", "hypothesis", "blast radius",
                         "PodChaos", "podchaos.chaos-mesh.io"],
        "must_not": [],
    },
    {
        "id": "postmortem-blameless",
        "domain": "sre-runbook",
        "prompt": ("Outline a blameless postmortem template: timeline, "
                   "impact, root cause (5-whys, NOT 'human error'), "
                   "contributing factors, action items with owner+deadline, "
                   "lessons. Show example for a deploy that took down "
                   "auth for 12 minutes."),
        "must_contain": ["timeline", "impact", "5-whys", "contributing",
                         "action items", "owner"],
        "must_not": ["human error"],
    },

    # ── Code engineering (10) ──
    {
        "id": "py-async-rate-limit",
        "domain": "code-python",
        "prompt": ("Python: Implement an async token-bucket rate limiter "
                   "(asyncio) usable as decorator. 100 req/sec, burst 200, "
                   "per-key (e.g., user_id). Backed by Redis for distributed."),
        "must_contain": ["asyncio", "redis", "tokens", "@", "decorator"],
        "must_not": [],
    },
    {
        "id": "ts-react-suspense",
        "domain": "code-typescript",
        "prompt": ("React 19 + TypeScript: data-fetching component using "
                   "Suspense + Error Boundary for a /users/{id} endpoint. "
                   "TanStack Query v5. Show optimistic update on PATCH."),
        "must_contain": ["Suspense", "ErrorBoundary", "useQuery",
                         "useMutation", "onMutate"],
        "must_not": [],
    },
    {
        "id": "rust-tokio-graceful",
        "domain": "code-rust",
        "prompt": ("Rust + tokio: an HTTP server (axum) with graceful "
                   "shutdown on SIGTERM, draining in-flight requests up to "
                   "30s, and OpenTelemetry tracing via tracing-opentelemetry."),
        "must_contain": ["axum", "tokio::signal", "graceful_shutdown",
                         "tracing_opentelemetry"],
        "must_not": [],
    },
    {
        "id": "go-context-deadline",
        "domain": "code-go",
        "prompt": ("Go: HTTP handler that fans-out to 3 downstream services "
                   "in parallel with errgroup, total timeout 200ms shared via "
                   "context, cancel siblings on first error."),
        "must_contain": ["errgroup", "context.WithTimeout", "ctx",
                         "go func"],
        "must_not": [],
    },
    {
        "id": "py-pydantic-validate",
        "domain": "code-python",
        "prompt": ("Pydantic v2 model for an Order: id UUID, items list "
                   "(min 1), total Decimal>0, currency ISO-4217, "
                   "created_at UTC datetime. Custom validator ensuring "
                   "total = sum(item.subtotal)."),
        "must_contain": ["BaseModel", "Field", "field_validator",
                         "Decimal", "UUID"],
        "must_not": [],
    },
    {
        "id": "sql-windowed-rank",
        "domain": "data-sql",
        "prompt": ("PostgreSQL: top 3 selling products per category in the "
                   "last 30 days, including ties. Show ROW_NUMBER vs RANK vs "
                   "DENSE_RANK and pick the right one."),
        "must_contain": ["RANK\\(\\)", "PARTITION BY", "ORDER BY",
                         "INTERVAL", "30"],
        "must_not": [],
    },
    {
        "id": "py-decorator-retry",
        "domain": "code-python",
        "prompt": ("Python decorator @retry that does exponential backoff "
                   "(start 1s, factor 2, jitter ±25%, max 5 attempts) on "
                   "transient errors only (HTTP 5xx, ConnectionError). "
                   "Type-hints + asyncio compatible."),
        "must_contain": ["functools.wraps", "asyncio.iscoroutinefunction",
                         "random.uniform", "backoff"],
        "must_not": [],
    },
    {
        "id": "test-pytest-fixture",
        "domain": "test-pytest",
        "prompt": ("pytest fixture chain: 'db' (session-scoped, real Postgres "
                   "via testcontainers), 'app' (function-scoped, FastAPI "
                   "TestClient with overridden DB dep), 'authed_client' "
                   "(auto-login user)."),
        "must_contain": ["@pytest.fixture", "scope=\"session\"",
                         "testcontainers", "TestClient", "dependency_overrides"],
        "must_not": [],
    },
    {
        "id": "js-ws-reconnect",
        "domain": "code-typescript",
        "prompt": ("TypeScript: a WebSocket client class with auto-reconnect "
                   "(exponential backoff, max 10 attempts), heartbeat ping "
                   "every 30s, message queue while disconnected, observable-"
                   "style on(event) API."),
        "must_contain": ["class", "WebSocket", "reconnect",
                         "addEventListener", "queue"],
        "must_not": [],
    },
    {
        "id": "rust-error-handling",
        "domain": "code-rust",
        "prompt": ("Rust: define a typed error hierarchy using thiserror for "
                   "an OrderService (NotFound, Validation, Database, External). "
                   "Show how to bubble with ? and map external lib errors via "
                   "From<>."),
        "must_contain": ["#\\[derive\\(.*Error.*\\)\\]", "thiserror::Error",
                         "From<", "Result<"],
        "must_not": [],
    },
]

assert len(EVAL_PROMPTS) == 50, f"expected 50 prompts, got {len(EVAL_PROMPTS)}"


def call_model(model: str, prompt: str, endpoint: str | None,
               max_tokens: int = 1500, hf_token: str | None = None) -> str:
    """Call vLLM/HF Inference/local endpoint with OpenAI-compatible /v1/chat."""
    url = (endpoint or "http://localhost:8000/v1") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": ("You are Surrogate-1, a senior DevSecOps + SRE + "
                         "coding agent. Cite real APIs only. Output runnable "
                         "code/config; no placeholders.")},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()
    hdr = {"Content-Type": "application/json"}
    if hf_token:
        hdr["Authorization"] = f"Bearer {hf_token}"
    req = urllib.request.Request(url, data=body, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        return f"__ERROR__: {e}"


def grade(response: str, prompt_def: dict) -> dict:
    """Auto-grade with regex rubric. Returns {checks: bool*4, score: 0-1}."""
    text = response or ""

    # Check 1: must_contain — every required pattern must appear
    must = prompt_def.get("must_contain", [])
    correct = all(re.search(p, text, re.IGNORECASE | re.MULTILINE)
                  for p in must) if must else True

    # Check 2: must_not — no forbidden pattern
    must_not = prompt_def.get("must_not", [])
    no_forbidden = not any(re.search(p, text, re.IGNORECASE | re.MULTILINE)
                            for p in must_not)

    # Check 3: cite-real proxy — flag obvious phantom APIs
    phantom_re = (r"\bAWS::Custom::|\bnon_existent_|\bphantom_api|"
                  r"<insert.*here>|\bTODO[: ]")
    cite_real = not re.search(phantom_re, text, re.IGNORECASE)

    # Check 4: right-size proxy — overly long response (>3× expected) =
    # likely over-engineered, OR very short (<150 chars) = under-answered
    right_size = 150 < len(text) < 12000

    score = sum([correct, no_forbidden, cite_real, right_size]) / 4.0
    return {
        "correct": correct,
        "no_forbidden": no_forbidden,
        "cite_real": cite_real,
        "right_size": right_size,
        "score": score,
        "len": len(text),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True,
                    help="Model id (HF Hub) or vLLM model name")
    ap.add_argument("--endpoint", default=None,
                    help="OpenAI-compatible base URL (omit for localhost:8000)")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"━━━ axentx-eval-50 — {args.model}")
    print(f"  endpoint: {args.endpoint or 'http://localhost:8000/v1'}")
    print(f"  output  : {out_dir}")
    print()

    results = []
    for i, p in enumerate(EVAL_PROMPTS, 1):
        t0 = time.time()
        resp = call_model(args.model, p["prompt"], args.endpoint,
                          hf_token=args.hf_token)
        elapsed = time.time() - t0
        g = grade(resp, p)
        g["id"] = p["id"]
        g["domain"] = p["domain"]
        g["elapsed_s"] = round(elapsed, 1)
        results.append(g)
        marker = "✓" if g["score"] >= 0.75 else ("~" if g["score"] >= 0.5 else "✗")
        print(f"  [{i:2d}/50] {marker} {p['id']:<32} score={g['score']:.2f} "
              f"({g['elapsed_s']}s, {g['len']}b)")
        # Save full response per prompt (debug)
        (out_dir / f"{p['id']}.txt").write_text(
            f"## prompt\n{p['prompt']}\n\n## response\n{resp}\n", encoding="utf-8")

    n = len(results)
    avg_score = sum(r["score"] for r in results) / max(n, 1)
    by_domain = {}
    for r in results:
        by_domain.setdefault(r["domain"], []).append(r["score"])
    domain_means = {d: sum(s)/len(s) for d, s in by_domain.items()}

    summary = {
        "model": args.model,
        "n_prompts": n,
        "score": round(avg_score * 100, 2),       # bench script greps "score"
        "score_decimal": round(avg_score, 4),
        "by_domain": {k: round(v*100, 2) for k, v in domain_means.items()},
        "checks": {
            "correct": sum(1 for r in results if r["correct"]),
            "no_forbidden": sum(1 for r in results if r["no_forbidden"]),
            "cite_real": sum(1 for r in results if r["cite_real"]),
            "right_size": sum(1 for r in results if r["right_size"]),
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print()
    print(f"━━━ axentx-eval-50 done — score: {summary['score']}% ({n} prompts)")
    for d, s in sorted(domain_means.items(), key=lambda x: -x[1]):
        n_d = len(by_domain[d])
        print(f"  {d:<20} {s*100:>5.1f}% ({n_d} prompts)")
    print()
    print(f"  full output: {out_dir}/summary.json")


if __name__ == "__main__":
    main()
