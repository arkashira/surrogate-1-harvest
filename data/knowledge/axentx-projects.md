# Axentx Projects — Complete Reference

> All projects in ~/axentx/ with tech, purpose, structure, status. Updated: 2026-04-16

---

## Active Projects (Git Repos)

### Costinel — Cloud Cost Governance Platform
**Tech**: React 18, Vite, TailwindCSS, Recharts, Supabase (PostgREST + GoTrue), PostgreSQL, Kong, Redis, Docker
**Purpose**: Multi-cloud cost visibility, intelligence, governance (AWS, GCP, Azure)

**Features**: Real-time cost dashboard, forecasting, heatmap, smart recommendations (RI, Savings Plans, rightsizing), Kanban case management, multi-org RBAC, Slack/Discord/Email notifications

**Entry**: `npm run dev` (Vite) | Deploy: `./scripts/deploy.sh`, `./scripts/deploy-supabase.sh`
**Env**: VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY, VITE_API_MODE (supabase|local)
**Latest**: v3.0.0 (GCP auth edge function, UI improvements)
**Size**: 265 MB | **Status**: Active

---

### Vanguard — Cloud Security Platform
**Tech**: FastAPI (Python 3.11+), React 18, Vite, TypeScript, SQLAlchemy, AsyncPG, PostgreSQL 15, Redis 7, Neo4j 5, MinIO, Celery, Prometheus
**Purpose**: Unified cloud security assessment, auditing, hardening (AWS, GCP, Azure)

**Features**: Security assessment, real-time compliance monitoring, hardening recommendations, graph-based security visualization, DefectDojo/Wazuh integration (optional)

**Entry**: Backend port 8000 (FastAPI), Frontend Vite dev
**Env**: DATABASE_URL, REDIS_URL, NEO4J_*, MINIO_*, SECRET_KEY, ENCRYPTION_KEY, CELERY_*
**Docker**: docker-compose.yml (PostgreSQL, Redis, Neo4j, MinIO, optional DefectDojo + Wazuh)
**Latest**: "implementation canarytoken"
**Size**: 17 MB | **Status**: Active

---

### AxiomOps — AI-Powered Enterprise Operations Decision Platform
**Tech**: Turbo monorepo (pnpm), React/Vite frontend, Node.js/TypeScript backend (9 services), PostgreSQL 16, Redis 7, NATS, Qdrant, OPA, Ollama (Qwen 7B), OpenAI (Planner), Anthropic Claude (Reviewer)
**Purpose**: Autonomous DevOps decision platform with Surrogate System

**Services**:
```
services/
├── surrogate/               # Main orchestrator
├── executor-k8s/           # Kubernetes executor
├── executor-terraform/     # Terraform executor
├── policy-gate/            # OPA policy enforcement (fail-closed)
├── rag-service/            # RAG (Qdrant)
├── evidence-collector/     # Prometheus/Loki/Tempo collector
├── runner-worker/          # Job runner
└── surrogate-consensus/    # AI consensus learning
```

**Observability**: Prometheus 9090, Grafana 3001, Loki 3100, Tempo 3200, AlertManager 9093

**Features**: Workflow orchestration (12 presets: CI/CD, Blue-Green, Canary, DB Migration, etc.), incident management with Perception Layer, root cause analysis (Sherlock), Intent Language Compiler (YAML -> IaC), OPA policy enforcement, multi-cloud (AWS, GCP, Azure), DevSecOps integrations (GitLab, Harbor, ArgoCD, Vault, DefectDojo, SonarQube)

**Entry**: Frontend port 8080, Surrogate API port 3010
**Env**: OPENAI_API_KEY, ANTHROPIC_API_KEY, AWS/GCP/Azure creds, POSTGRES_PASSWORD, JWT_SECRET, NATS_URL, OPA_URL, PROMETHEUS_URL, GITLAB_TOKEN, HARBOR_*, ARGOCD_TOKEN, VAULT_*
**Size**: 1.3 GB | **Status**: Active

---

### Basic-Data-Science — ML Housing Price Prediction
**Tech**: Python, Pandas 2.3, Matplotlib, Seaborn, Jupyter
**Purpose**: California housing price prediction (EDA + ML modeling)
**Entry**: `notebooks/main.ipynb` | Data: `data/raw/house_california.xlsx`
**Dataset**: 20,640 housing records
**Size**: 4.4 MB | **Status**: Active

---

### Surrogate-1 — Custom LLM Fine-Tuning
**Tech**: GLM-5 (744B/40B MoE, FP8) on Huawei Cloud, QLoRA via LLaMA-Factory, Huawei Ascend 910B, RL (slime framework), RAG (Qdrant), LangGraph (MCP/ACP)
**Purpose**: Fine-tuned LLM specialist for Coding, DevSecOps, SRE, Platform Engineering

**Structure**:
```
scripts/data_prep/     # collect_public_data.py, convert_to_jsonl.py
scripts/training/      # finetune.py
scripts/rag/           # RAG deployment
config/                # training_config.yaml, lora_config.yaml, inference_config.yaml, qdrant_config.yaml
```

**Deps**: transformers>=4.46.0, peft>=0.12.0, bitsandbytes>=0.43.0, accelerate>=0.33.0, trl>=0.9.0
**Cost**: ~$70-100 setup, ~$30-60/month operational
**Size**: 160 KB | **Status**: Active

---

## Non-Git Projects

### Arkship — DevSecOps/SRE Platform with AI (LARGEST)
**Tech**: FastAPI (Python 3.11+), React 18/Vite, Temporal, Kubernetes, Neo4j, Qdrant, NATS, OPA, MinIO, AI ensemble (Qwen 7B + Mistral + DeepSeek-R1), Prometheus/Grafana/Loki/Tempo
**Purpose**: Complete infrastructure automation, incident management, AI-powered DevOps

**Structure**:
```
arkship/
├── api/           # FastAPI backend (130+ Python modules)
├── ui/            # React/Vite frontend
├── training/      # ML training (Torch, Transformers, PEFT, WandB)
├── surrogate/     # Independent AI service
├── docker/        # Multi-service Dockerfiles
├── config/        # Prometheus, Grafana, AlertManager
├── blueprints/    # Infrastructure templates
├── policy/        # OPA policies
├── workflows/     # Temporal workflows
└── ide-integration/  # IDE plugins
```

**Entry**: Frontend :3000, Arkship API :8000, Surrogate AI :8001
**Features**: Temporal workflow orchestration (12 presets), incident management (Perception Layer, Sherlock), Intent Language Compiler (YAML -> Terraform/Crossplane/CF), 6 AI roles (Guardian, Navigator, Assembler, Sherlock, Auditor, Coach), 15 knowledge domains, Neo4j knowledge graph (250+ tools), Qdrant vector store (130+ repos), consensus learning, auto-training 24/7
**Size**: 12 GB | **Status**: Active (not in git)

---

### Workio — HR Time Tracking via LINE OA
**Tech**: React 18, Vite, TypeScript, TailwindCSS (frontend), Node.js/Express/TypeScript (backend), PostgreSQL, LINE Messaging API, Vercel
**Purpose**: Multi-tenant time tracking — clock in/out via LINE Official Account, leave/OT requests, advance payments, GPS verification

**Features**: Clock in/out via LINE OA (@774rhjqd), GPS location verification, leave/OT request workflows, advance payment tracking, holiday management, dashboard/reports, multi-tenant RBAC (SuperAdmin, Admin, Manager, Employee)

**Structure**:
```
workio/workio/
├── src/pages/          # Dashboard, Employees, LeaveRequests, OTRequests, AdvancePayments, Tenants, Holidays, Settings
├── src/lib/            # api.ts, superadminApi.ts, supabase.ts
├── server/src/         # Express backend (routes, middleware, services, db/schema.sql)
├── line/               # richmenu.json, LINE integration
└── .github/workflows/  # deploy.yml, ai-assistant.yml, ai-commit.yml, pr-review.yml
```

**Env**: DATABASE_URL, LINE_CHANNEL_ID/SECRET/ACCESS_TOKEN, JWT_SECRET, API_URL, FRONTEND_URL
**Deploy**: Vercel (frontend), self-hosted (backend)
**Size**: 319 MB | **Status**: Deployed (not in git)

---

### Surrogate — AI Training Toolkit
**Tech**: Python, Transformers, PEFT, bitsandbytes
**Purpose**: Core training framework for fine-tuning, referenced by other projects
**Size**: 60 KB | **Status**: Framework (not in git)

---

### AI — Huawei Cloud CLI + Surrogate Tests
- `huawei/` — Huawei Cloud CLI (KooCLI) documentation (Go-based)
- `surrogate-1/` — Mirror/dev version with tests (unit/integration/e2e)
**Size**: 4.1 MB | **Status**: Mixed

---

### Empty
- `social/` (fb-autounfriend placeholder)
- `mkdir/` (accidental artifact)

---

## Summary

| Project | Git | Tech | Purpose | Status |
|---------|-----|------|---------|--------|
| Costinel | Y | React/Supabase | Cloud Cost Governance | Active |
| Vanguard | Y | FastAPI/React/Neo4j | Cloud Security | Active |
| AxiomOps | Y | Node.js/React/AI | DevOps AI Decision | Active |
| Basic-Data-Science | Y | Python/Jupyter | ML Housing Prediction | Active |
| Surrogate-1 | Y | Python/GLM-5 | Custom LLM Fine-tuning | Active |
| Arkship | N | Python/React/Temporal | Infrastructure Automation + AI | Active |
| Workio | N | React/Express/LINE API | HR Time Tracking via LINE OA | Deployed |
| Surrogate | N | Python/Transformers | AI Training Toolkit | Framework |

**Patterns**: Multi-service microservices, full LGTM observability, AI-augmented operations, Docker-native, DevSecOps focus, custom LLM training (Huawei Cloud)
