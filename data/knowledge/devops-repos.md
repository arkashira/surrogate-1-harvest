# DevOps Repository Map — Complete Reference

> Every repo in ~/develope/DevOps/ with purpose, tech, key files, and status.
> Updated: 2026-04-16

---

## Core CI/CD Repositories

### thinkbit-devops-material/
- **Purpose**: Centralized CF templates, buildspec configs, CI/CD definitions
- **Tech**: CloudFormation, CodeBuild, Python, Bash
- **Key dirs**: `ci/` (CI templates), `cd/` (CD templates), `buildspec/` (universal buildspecs)
- **Status**: Active

### thinkbit-devops-modules/
- **Purpose**: Per-service params, buildspecs, templates (staging)
- **Tech**: JSON params, CloudFormation, CodeBuild
- **Key dir**: `modules/{stack-name}/{stack-name}-params.json`
- **Modules**: excise-wine-authen-staging, excise-wine-nodejs-api[-staging], excise-wine-go-api[-staging], excise-wine-proxy, excise-car-backend-staging, excise-car-cron[-staging], thinkbit-devops-jenkins
- **Status**: Active

### thinkbit-devops-modules-prod/
- **Purpose**: Production CF stack orchestration via Sceptre
- **Tech**: Sceptre, Jinja2, Python, CloudFormation, Jenkins, GitHub Actions
- **Key files**: `sceptre/`, `Jenkinsfile` (140 lines), `modules.yaml`, `scripts/`
- **Pipeline**: Checkout -> Sceptre -> Parse modules -> Sync -> Promote -> Policy -> Diff -> Migrate -> Deploy -> SLO -> Status
- **Deploy regions**: apse1 (default), apse7 (deploy)
- **Status**: Active (production)

### thinkbit-devops-pipeline/
- **Purpose**: GitHub Actions reusable workflows for all repos
- **Tech**: GitHub Actions, YAML
- **Features**: Auto-detect language, GitLeaks/Snyk/Semgrep, Docker (ECR/GHCR), Playwright E2E, Discord/Slack notifications
- **Key files**: `.github/workflows/main.yml`, `build-template.yml`, `security-template.yml`, `quality-template.yml`
- **Status**: Active

---

## Infrastructure as Code

### thinkbit-devops-iac/ — AWS Terraform
- **Purpose**: Core AWS infrastructure import & management (dev/staging/prod)
- **Tech**: Terraform 1.0+, AWS provider v5.80
- **Backend**: S3 (`thinkbit-terraform-state`, apse1), DynamoDB lock
- **Workspaces**: dev, staging, prod
- **Resources managed**:
  - VPC & Subnets (imported)
  - Security Groups (imported)
  - RDS Instances (SQL Server, db.m6i.2xlarge, apse7, 300GB gp3)
  - RDS Proxy (creates & manages with Secrets Manager)
  - EC2 Instances (t3.large/xlarge)
  - ALB (imported)
  - Lambda (classic_car_email — Python 3.13, SNS-to-Discord)
  - DynamoDB (classic_car_email table)
  - SNS Topics
- **Modules**: `modules/rds-proxy/`, `modules/security-groups/`, `modules/s3/`, `modules/sns/`, `modules/alb/`
- **Config**: `environments/dev/`, `environments/staging/`, `environments/prod/` terraform.tfvars
- **Status**: Active

### gcp-terraform/ — GCP Terraform
- **Purpose**: Google Cloud Platform resource management
- **Tech**: Terraform, GCP provider v5.45.2
- **Key files**: `main-new-project.tf` (5,170 lines), `backend-new-project.tf`
- **Environments**: `environments/prod.tfvars`, `environments/staging.tfvars`, `environments/staging-new-project.tfvars`
- **Resources**: GCP projects, Compute Engine VMs, Cloud SQL, Cloud Storage, VPC, Service Accounts
- **Plans**: `singapore-ohv2.tfplan`, `singapore-new-vms.tfplan`
- **Status**: Active

### azure-terraform/ — Azure Terraform
- **Purpose**: Azure cloud deployment & MFA portal
- **Tech**: Terraform, Azure provider v3.117.1, Node.js, SQLite/PostgreSQL

**Sub-projects:**
1. **mfa-portal/** — MFA Portal on Azure App Service + SQL Database + KeyVault
2. **tb-ai-mfa-portal-gen2/** — Next-Gen MFA Portal (Full Stack)
   - Backend: Python Flask/FastAPI with AI/ML pipeline (Azure OpenAI GPT-4)
   - Pipeline blocks: AI summarize, draft, classify, review, OCR, translate, PDF gen, SMS, scheduler, webhook, signature verify, e-booking
   - Frontend: HTML/CSS/JavaScript
   - Database: SQLite (demo) or PostgreSQL (production)
- **Status**: Active

### thinkbit-devops-cloudvpn/ — Pritunl VPN
- **Purpose**: VPN server for 30 users
- **Tech**: Terraform, AWS EC2 (t3.medium), Pritunl, MongoDB
- **Files**: `main.tf` (VPC, subnets), `ec2.tf`, `security_groups.tf`, `user_data.sh`
- **Cost**: ~$35-50/month
- **Status**: Active

### thinkbit-devops-SwithRole/ — Multi-Account IAM
- **Purpose**: Cross-account access with AWS Organizations
- **Tech**: Terraform, AWS IAM, AWS Organizations
- **Modules**: `organization/`, `iam-user/`, `member-account/`, `security/`, `cost-management/`
- **Features**: Multi-account org, SCP guardrails, CloudTrail centralization, budget alerts
- **Status**: Active

---

## Security & Compliance

### AWS-Fix-Assassment/ — Prowler Security Remediation
- **Purpose**: AWS security compliance remediation
- **Tech**: Terraform, Prowler v5.19.0, CloudFormation
- **Metrics (Apr 5, 2025)**: 5,759 total findings, 1,964 FAIL (-16.5% improvement)
- **Critical issues**:
  - Root account with 2 active access keys
  - 7 IAM users with AdministratorAccess
  - 85 ECS task defs with plaintext secrets
  - 9 Lambda functions with hardcoded secrets
  - 2 S3 buckets publicly accessible
- **Terraform modules**: `01-iam-security/`, `02-s3-security/`
- **Rollback**: `environments/rollback/rollback-phase1-only.tfvars`, `rollback-all.tfvars`
- **Status**: In-progress (~67% complete)

### thinkbit-devops-prowler/ — Security Scanning
- **Purpose**: Monthly AWS security compliance scanning
- **Tech**: AWS CDK (Python), Prowler
- **Schedule**: 2nd Sunday of each month
- **Regions**: apse1 & apse7
- **Checks**: 203+ security checks
- **Status**: Active

### thinkbit-devops-sonarqube/ — Code Quality
- **Purpose**: SonarQube server for code quality metrics
- **Tech**: Docker (SonarQube + PostgreSQL)
- **Files**: `docker-compose.yaml`, `buildspec.yaml`, `appspec.yaml`
- **Status**: Active

---

## CI/CD Platforms

### thinkbit-devops-jenkins/ — Jenkins Server
- **Purpose**: On-premises CI/CD platform
- **Tech**: Docker, Jenkins LTS
- **Dependencies**: Node.js 20, Python 3, AWS CLI v2, Ruby/Bundler (Fastlane), Playwright, Chromium
- **Pipelines**: `pipelines/devops-release/`, `excise-wine/`, `qa-classiccar/`, `qa-wine/`
- **Deploy**: `appspec.yml` (CodeDeploy), `docker-compose.yaml`
- **Status**: Active

---

## Data & Backup

### backup/ — Database Sync & Snapshots
- **Purpose**: RDS snapshots, prod->UAT sync, audit logging
- **Tech**: Python, Bash, AWS RDS, systemd
- **Key scripts**:
  - `rds-snapshot-daily.sh` + systemd timer — daily automated snapshots
  - `sync-prod-to-uat.py` + `offset-based-sync.py` — incremental DB sync
  - `find-deleted-records.py`, `find-who-deleted.py` — audit trail
- **Status**: Active

### bucket-sync/ — S3 <-> GCS Sync
- **Purpose**: Bi-directional cloud storage sync
- **Tech**: gsutil, rclone, Python, AWS DataSync
- **Scripts**: `run-sync.sh`, `verify-sync.sh`, `direct-transfer.py`
- **Status**: Active

### database-backup-service/
- **Status**: Empty placeholder

---

## Testing & Monitoring

### wine-loadtest-k6/ — Load Testing
- **Purpose**: K6-based load testing for Wine API
- **Tech**: K6, JavaScript, HTML reporting
- **Tests**: 5000-50k users, 10-minute sustained
- **Thresholds**: 95% response < 5s, failure rate < 10%
- **Reports**: `reports/summary-main-api.html`, `summary-go-api.html`, `summary-frontend-login.html`
- **Status**: Active

### Cost/ — AWS & GCP Inventory
- **Purpose**: Resource inventory & cost estimation
- **Tech**: Python, boto3, Google Cloud SDK
- **Scripts**: `aws_inventory.py` (600+ lines), `gcp_inventory.py`
- **Output**: CSV with service, resource ID, type, region, cost estimate, recommendation
- **Status**: Active

### thinkbit-devops-costcenter/
- **Status**: Placeholder with .kiro metadata

---

## Utilities & Reference

### script/ — AWS Utility Scripts
- `list-service-aws-tags.sh` — List all AWS resources & tags
- `create-ec2-temp.sh` — Temporary EC2 instance
- `fix-cloudwatch-retention.sh` — Log retention management

### credential/ — Security Keys & Certificates
- **SSH Keys**: `thinkbit-key.pem`, `think-bit-aws-th.pem`, `thinkbit-devops-key.pem`, `tbit-ai-credential`
- **SSL Certs**: `Thinkbit-excise-key/` (wildcard *.excise.go.th, DigiCert CA chain)
- **Service Account**: `service-account.json` (GCP/Firebase)
- **Apple**: `AuthKey_N8463W5926.p8` (App Store)

### Rollback-script/ — GKE Cluster Backup
- **Purpose**: Kubernetes GKE backup & restore
- **Tech**: Terraform, Kubernetes YAML, GCP
- **Exports**: `cluster-all-resources.yaml`, `cluster-services.yaml`, `cluster-pvc.yaml`, etc.

### gcp-info/ — GCP Reference Docs
- CloudFront integration guides, WAF/CDN recommendations
- GCP vs AWS database comparison
- API parameter optimization configs
- Traffic analysis dashboards (HTML)

### sherlock/ — ML Training Data
- **Purpose**: Arkship training data, ML consensus, policy repository
- **Data**: JSON consensus batches (batch_001-008)

### MFA/ — Alfresco on Kubernetes
- **Purpose**: Alfresco Content Services deployment
- **Tech**: Kubernetes, Helm, Kustomize, PostgreSQL
- **Status**: Reference/archived

### devops-exercises/ — Learning Materials
- DevOps practice exercises, multi-language docs
- **Status**: Archive/reference

### cf-vpn/
- **Status**: Empty placeholder
