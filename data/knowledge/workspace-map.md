# Complete Workspace Map

> Every project, every repo, where everything is. Updated: 2026-04-16

---

## ~/develope/ — Main Workspace

### Excise/ — Government Excise Department Systems

#### Wine/ — Wine Excise Management

> See detailed reference: [[AI-Hub/knowledge/excise-services|Excise Services Detail]]

| Directory | Tech | Purpose | Status |
|-----------|------|---------|--------|
| `excise-wine-authen/` | Node.js 20, Lambda, Cognito | Authentication (Firebase -> Cognito migration) | Active |
| `excise-wine-nodejs-api/` | Node.js 22, Express 5.1, MSSQL/Sequelize | Main REST API (v6) | Active |
| `excise-wine-go-api/` | Go 1.24, Gorilla Mux, Firestore | Go REST API | Active |
| `excise-wine-proxy/` | Nginx, EC2 c7g.large, CodeDeploy | Reverse proxy (ARM64 Graviton) | Active |
| `excise-wine-api/` | Node.js 22, Express 5.1, Vertex AI | Legacy API | Legacy |
| `excise-wine-python-api/` | Python 3.13, Flask, Algolia | Python search API | DEPRECATED |
| `excise-wine-frontend/` | React, Vite | Web UI | Active |
| `excise-wine-fasttrack-frontend/` | React | Fast-track variant UI | Active |
| `excise-wine-fasttrack-mobile/` | Flutter (Dart), Fastlane | Mobile app (iOS/Android/Web) | Active |

#### Car/ — Classic Car Excise Tracking
| Directory | Tech | Purpose | Status |
|-----------|------|---------|--------|
| `excise-car-backend/` | Node.js 20, Express 4.21, Prisma/MSSQL | Backend API (v2.2.22) | Active |
| `excise-car-cron/` | Node.js, TypeScript, Prisma, Webpack | Lambda cron (getCurrency, autoRejection) | Active |
| `excise-car-cron-staging/` | Node.js, TypeScript, Prisma | Staging cron variant | Active |
| `elephant/` | Node.js, Express 5.2, Prisma 7.2 | Dev/testing variant (v2.2.13) | Dev |

---

### DevOps/ — Infrastructure & Operations (30+ repos)

See detailed reference: [[AI-Hub/knowledge/devops-repos|DevOps Repos Map]]
See Terraform details: [[AI-Hub/knowledge/terraform|Terraform Reference]]

**Core repos:**
| Directory | Purpose | Tech |
|-----------|---------|------|
| `thinkbit-devops-material/` | CF templates, buildspec templates | CloudFormation |
| `thinkbit-devops-modules/` | Per-service params & buildspecs (staging) | JSON, CF |
| `thinkbit-devops-modules-prod/` | Production Sceptre orchestration | Sceptre, Jenkins |
| `thinkbit-devops-iac/` | Core AWS infra (Terraform) | Terraform, AWS |
| `thinkbit-devops-jenkins/` | Jenkins CI/CD server | Docker, Jenkins |
| `thinkbit-devops-pipeline/` | GitHub Actions reusable workflows | GitHub Actions |
| `thinkbit-devops-prowler/` | Monthly security scans | CDK, Prowler |
| `thinkbit-devops-sonarqube/` | Code quality analysis | Docker, SonarQube |
| `gcp-terraform/` | GCP infrastructure | Terraform, GCP |
| `azure-terraform/` | Azure infra + MFA portals | Terraform, Azure |
| `thinkbit-devops-cloudvpn/` | Pritunl VPN | Terraform |
| `thinkbit-devops-SwithRole/` | Multi-account IAM | Terraform |
| `AWS-Fix-Assassment/` | Security remediation | Terraform, Prowler |
| `backup/` | RDS snapshots, prod->UAT sync | Python, Bash |
| `bucket-sync/` | S3 <-> GCS sync | gsutil, rclone |
| `wine-loadtest-k6/` | Load testing (50k users) | K6 |
| `Cost/` | AWS/GCP inventory & costing | Python |
| `credential/` | SSH keys, SSL certs, service accounts | Files |

---

### AI/ — Oil Bills OCR System
- **Tech**: Python 3.8+, FastAPI, Azure OpenAI (GPT-4 Vision), Azure Document Intelligence
- **Purpose**: PDF -> JSON document processing for Thai oil tax bills
- **Key**: `src/ocr_model/`, `src/form_extractor/` (6 form types), `src/agent_checker/` (LLM verification)
- **Deploy**: Docker, port 8000
- **Status**: Active

### QA/ — Wine App E2E Tests
- **Tech**: TypeScript, Playwright v1.57.0
- **Purpose**: E2E automation tests for Wine management app
- **Pattern**: Page Object Model
- **Tests**: Register, Login, Add/Delete Liquor, User Management, Password Change
- **CI**: `.github/workflows/playwright.yml`
- **Status**: Active

### RD/ — Revenue Taxpayer System
- **Tech**: TypeScript, Azure Functions v4.3, Cosmos DB, Azure Doc Intelligence
- **Purpose**: Tax form processing (Form 1, 3, 30) + OCR validation + PDF export
- **Components**: `revenue_taxpayer_api/` (Azure Functions), `revenue_taxpayer_web/` (frontend), `document-intel/`
- **Status**: Active

### thinkbit/ — Multi-Project Platform
- **Components**:
  - `excise-car-backend/` — Node.js/TS, Prisma ORM (MSSQL), PM2 | Active
  - `firebase-projects-backup/` — 7 Firebase project configs | Archive
  - `think-bit.org/` — WordPress | Deprecated
  - Firebase utility scripts (export, view, storage)
- **Status**: Mixed (backend active, others archive)

### MFA/ — Empty placeholder
### Winmed/ — Empty placeholder

---

## ~/axentx/ — Axentx Projects

> See detailed reference: [[AI-Hub/knowledge/axentx-projects|Axentx Projects Map]]

| Directory | Tech | Purpose | Status |
|-----------|------|---------|--------|
| `Costinel/` | React, Supabase, PostgreSQL, Kong, Redis | Cloud Cost Governance (AWS/GCP/Azure) | Active |
| `Vanguard/` | FastAPI, React, Neo4j, PostgreSQL, Redis, Celery | Cloud Security Platform | Active |
| `axiomops/` | Node.js/TS (Turbo monorepo), React, OpenAI, Claude, NATS, Qdrant, OPA | AI-Powered DevOps Decision Platform | Active |
| `arkship/arkship/` | FastAPI, React, Temporal, K8s, Neo4j, Qdrant, AI ensemble | Infrastructure Automation + AI (12 GB) | Active (no git) |
| `surrogate-1/` | Python, GLM-5, QLoRA, LLaMA-Factory, Huawei Cloud | Custom LLM Fine-tuning for DevOps | Active |
| `basic-data-science/` | Python, Pandas, Jupyter | ML Housing Price Prediction | Active |
| `workio/` | React, TypeScript, Vite, Vercel | Web Application | Deployed |
| `surrogate/` | Python, Transformers, PEFT | AI Training Toolkit | Framework |
| `workio/` | React, Express, TypeScript, LINE API, PostgreSQL | HR Time Tracking via LINE OA | Deployed |
| `AI/` | Go (Huawei CLI), Python (Surrogate tests) | Mixed utilities | Mixed |

---

## Quick Lookup Table

| What | Where |
|------|-------|
| Wine API code | `~/develope/Excise/Wine/excise-wine-nodejs-api/` |
| Wine Go API | `~/develope/Excise/Wine/excise-wine-go-api/` |
| Wine Auth (Cognito) | `~/develope/Excise/Wine/excise-wine-authen/` |
| Wine Proxy (Nginx) | `~/develope/Excise/Wine/excise-wine-proxy/` |
| Car Backend | `~/develope/Excise/Car/excise-car-backend/` |
| Car Cron | `~/develope/Excise/Car/excise-car-cron/` |
| CF Templates | `~/develope/DevOps/thinkbit-devops-material/ci/` + `cd/` |
| Buildspec Templates | `~/develope/DevOps/thinkbit-devops-material/buildspec/` |
| Service Params | `~/develope/DevOps/thinkbit-devops-modules/modules/` |
| Prod Deploy (Sceptre) | `~/develope/DevOps/thinkbit-devops-modules-prod/sceptre/` |
| AWS Terraform | `~/develope/DevOps/thinkbit-devops-iac/` |
| GCP Terraform | `~/develope/DevOps/gcp-terraform/` |
| Azure Terraform | `~/develope/DevOps/azure-terraform/` |
| Jenkins Server | `~/develope/DevOps/thinkbit-devops-jenkins/` |
| Security Scans | `~/develope/DevOps/thinkbit-devops-prowler/` |
| Security Remediation | `~/develope/DevOps/AWS-Fix-Assassment/` |
| Load Tests | `~/develope/DevOps/wine-loadtest-k6/` |
| E2E Tests | `~/develope/QA/` |
| OCR System | `~/develope/AI/` |
| Tax Forms | `~/develope/RD/` |
| SSH Keys | `~/develope/DevOps/credential/` |
| SSL Certs | `~/develope/DevOps/credential/Thinkbit-excise-key/` |
| DB Backups | `~/develope/DevOps/backup/` |
| Cost Analysis | `~/develope/DevOps/Cost/` |
| VPN Server | `~/develope/DevOps/thinkbit-devops-cloudvpn/` |
| Costinel (Cost Gov) | `~/axentx/Costinel/` |
| Vanguard (Security) | `~/axentx/Vanguard/` |
| AxiomOps (AI DevOps) | `~/axentx/axiomops/` |
| Arkship (Infra+AI) | `~/axentx/arkship/arkship/` |
| Surrogate-1 (LLM) | `~/axentx/surrogate-1/` |
| Claude Memory | `~/.claude/memory/` |
| AI Knowledge Hub | `~/Documents/Obsidian Vault/AI-Hub/` |

---

## Cloud Summary

| Cloud | Region | Purpose | Terraform |
|-------|--------|---------|-----------|
| AWS | ap-southeast-1 (Singapore) | Build, CI/CD, CodeBuild | thinkbit-devops-iac |
| AWS | ap-southeast-7 (Bangkok) | Deploy, ECS, Lambda, RDS | thinkbit-devops-iac |
| GCP | asia-southeast1 (Singapore) | VMs, Cloud SQL, Storage | gcp-terraform |
| Azure | (configured) | MFA Portal, AI Portal | azure-terraform |
| Huawei | (configured) | LLM Fine-tuning (ModelArts, Ascend 910B) | surrogate-1 |
