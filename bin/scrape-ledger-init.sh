#!/usr/bin/env bash
# Initialize global scrape ledger — single source of truth for "what's been scraped"
# All scrapers check ledger before scraping + write after.
# DB: ~/.surrogate/state/scrape-ledger.db  (SQLite WAL for concurrent safety)
set -u
DB="$HOME/.surrogate/state/scrape-ledger.db"
mkdir -p "$(dirname "$DB")"

sqlite3 "$DB" <<'SQL'
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS scraped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'github', 'rss', 'stackoverflow', 'fs', 'crawl4ai'
    identifier TEXT NOT NULL,       -- 'owner/repo' or URL or file path hash
    domain TEXT,                    -- 'security', 'devops', 'ai-ml', 'frontend', etc.
    subdomain TEXT,                 -- 'cve', 'kyverno', 'observability', etc.
    language TEXT,                  -- 'python', 'go', 'terraform'
    stars INTEGER DEFAULT 0,
    scraped_at TEXT NOT NULL,
    pairs_written INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok',       -- 'ok', 'err', 'skipped', 'partial'
    notes TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scraped_src_id ON scraped(source, identifier);
CREATE INDEX IF NOT EXISTS idx_scraped_domain ON scraped(domain);
CREATE INDEX IF NOT EXISTS idx_scraped_ts ON scraped(scraped_at);

-- Domain taxonomy — what every enterprise software company deals with
CREATE TABLE IF NOT EXISTS domain_taxonomy (
    domain TEXT PRIMARY KEY,
    subdomain TEXT,
    search_keywords TEXT,
    priority INTEGER DEFAULT 5,      -- 1=critical, 10=nice-to-have
    target_repos INTEGER DEFAULT 100
);

-- Seed taxonomy
INSERT OR IGNORE INTO domain_taxonomy (domain, subdomain, search_keywords, priority, target_repos) VALUES
-- CODING (per language)
('coding','python-framework','fastapi django flask poetry uv ruff mypy pydantic',1,150),
('coding','python-async','asyncio aiohttp httpx anyio trio',1,80),
('coding','typescript-framework','nextjs remix astro svelte solid react vue nuxt',1,150),
('coding','typescript-tooling','vite tsup esbuild turbopack biome',2,80),
('coding','go-ecosystem','gin echo fiber chi gorilla cobra viper',1,120),
('coding','rust-ecosystem','tokio axum actix warp rocket serde clap',1,100),
('coding','java-kotlin','spring boot ktor micronaut quarkus',2,80),
('coding','mobile-native','swiftui jetpack compose react-native flutter',2,100),
-- SECURITY
('security','appsec','owasp top10 cwe sast dast semgrep bandit eslint-security',1,120),
('security','cloudsec','prowler scoutsuite cloudcustodian checkov tfsec iam-cli',1,120),
('security','container-sec','trivy grype syft kyverno opa falco tetragon',1,100),
('security','supply-chain','cosign sigstore slsa sbom cyclonedx in-toto',1,80),
('security','secrets','vault sops age gitleaks trufflehog detect-secrets',1,60),
('security','identity','keycloak authentik ory hydra dex oidc-provider',2,60),
('security','detection','sigma mitre-attack falco-rules wazuh yara sentinelone',1,80),
('security','offensive','metasploit nuclei gobuster ffuf burp-extensions',3,40),
-- OPS / DEVOPS / SRE
('ops','devops-ci','github-actions gitlab-ci jenkins dagger buildkit',1,100),
('ops','iac','terraform pulumi cdk cloudformation ansible',1,150),
('ops','kubernetes','k8s helm kustomize argocd flux crossplane istio linkerd',1,200),
('ops','sre','sre-book postmortem slo burn-rate chaos-engineering',1,80),
('ops','chaos','chaos-mesh litmus gremlin chaos-toolkit',2,40),
('ops','config-mgmt','ansible chef puppet salt',3,40),
('observability','metrics','prometheus thanos mimir victoriametrics alertmanager',1,100),
('observability','logs','loki elasticsearch opensearch fluentbit vector',1,80),
('observability','traces','tempo jaeger zipkin skywalking honeycomb',1,80),
('observability','apm','datadog newrelic dynatrace appdynamics instana',2,40),
('observability','profiling','pyroscope parca gprofiler py-spy flamegraph',2,40),
('observability','otel','opentelemetry-collector otel-sdk semantic-conventions',1,60),
('observability','ebpf','cilium tetragon pixie falco inspektor-gadget',1,60),
-- CLOUD
('cloud','aws','aws-cdk aws-samples aws-solutions aws-copilot sam',1,200),
('cloud','gcp','gcp-samples terraform-google anthos',1,100),
('cloud','azure','azure-samples bicep terraform-azurerm',1,100),
('cloud','multicloud','crossplane cluster-api karpenter external-dns',2,60),
('cloud','serverless','sam sst cdk serverless-framework workers wrangler',1,100),
('finops','finops','kubecost opencost cloudhealth crane infracost',1,60),
-- AI / ML / AGENTS
('ai','llm-serving','vllm tgi ollama llama.cpp exllama sglang',1,100),
('ai','llm-training','unsloth axolotl peft trl ms-swift torchtune',1,100),
('ai','agents','langgraph crewai autogen mcp-server dspy haystack',1,120),
('ai','rag','llamaindex langchain colbert chroma qdrant weaviate',1,100),
('ai','ml-frameworks','pytorch-lightning jax equinox flax transformers diffusers',2,80),
('ai','ml-ops','mlflow wandb comet kedro zenml',2,60),
('ai','eval','lm-evaluation-harness deepeval ragas opik',2,40),
-- DATA
('data','databases','postgres mysql pgvector cockroachdb tidb',1,100),
('data','streaming','kafka nats redpanda pulsar flink',1,80),
('data','warehouses','clickhouse duckdb snowflake trino presto starrocks',1,80),
('data','orchestration','airflow prefect dagster temporal',1,80),
('data','formats','parquet iceberg delta-lake hudi avro',2,40),
('data','etl','dbt meltano singer airbyte',2,40),
-- FRONTEND / UX
('frontend','components','shadcn-ui radix headlessui mantine chakra',2,80),
('frontend','state','zustand jotai redux-toolkit tanstack-query swr',2,60),
('frontend','styling','tailwindcss unocss vanilla-extract stitches',2,60),
('frontend','animations','framer-motion auto-animate gsap lottie',3,40),
-- BACKEND
('backend','graphql','apollo relay urql hasura postgraphile',2,60),
('backend','grpc','grpc-web buf connect-go',2,40),
('backend','queues','bullmq sidekiq celery rq',2,60),
-- ARCHITECTURE
('architecture','patterns','hexagonal ddd cqrs event-sourcing saga outbox',1,60),
('architecture','messaging','cloudevents asyncapi schema-registry',2,40),
-- QUALITY / TESTING
('quality','unit-test','pytest vitest jest junit5 testify',2,60),
('quality','e2e','playwright cypress puppeteer selenium',2,60),
('quality','load-test','k6 locust gatling vegeta',2,40),
('quality','contract','pact dredd schemathesis',3,30),
-- COMPLIANCE
('compliance','audit','pdpa gdpr soc2 iso27001 pci-dss hipaa',1,60),
('compliance','policy-as-code','opa kyverno gatekeeper conftest',1,60),
-- PRODUCT / BUSINESS
('product','analytics','posthog plausible amplitude mixpanel',2,40),
('product','feature-flags','unleash flagsmith growthbook launchdarkly',2,40);

SELECT 'ledger initialized: ' || COUNT(*) || ' domains' FROM domain_taxonomy;
SQL

echo "✅ Ledger at $DB"
