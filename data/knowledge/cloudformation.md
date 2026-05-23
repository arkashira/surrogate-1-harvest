---
name: CloudFormation Stack Creation Knowledge
description: Complete guide to creating CF stacks — templates, flow, naming, S3 structure, params, all stacks inventory with regions
type: reference
updated: 2026-04-16
---

# CloudFormation Stack Creation — Complete Knowledge

## Stack Naming Convention

```
{org}-{project}-{module}[-{environment}]
```
- `org`: excise, thinkbit
- `project`: wine, car, devops
- `module`: authen, nodejs-api, go-api, proxy, backend, cron, jenkins
- `environment`: staging, production (omitted = production)

## Stack Types & Creation Order

Every service requires stacks created in this exact order:

### 1. State Stack (apse7 or apse1)
**Template**: `ci/cloudformation-state-github.yaml`
**Purpose**: Creates GitHub repo via Lambda custom resource + stores config in SSM
**Resources**: Lambda function (GitHub repo creator), IAM role, SSM parameters
**Region**: ap-southeast-7 (for services) or ap-southeast-1 (for CI tools)
**Naming**: `{stack-name}-state-stack`

```bash
aws cloudformation create-stack \
  --stack-name excise-wine-authen-staging-state-stack \
  --template-body file://ci/cloudformation-state-github.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=excise \
    ParameterKey=Project,ParameterValue=wine \
    ParameterKey=Module,ParameterValue=authen-staging \
    ParameterKey=GitHubBranch,ParameterValue=staging \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region ap-southeast-1
```

### 2. ECR State Stack (apse7 only — ECS services only)
**Template**: `ci/cloudformation-ecr-state.yaml`
**Purpose**: Creates ECR repository with lifecycle policy (keep 10 images)
**Resources**: ECR::Repository
**Region**: ap-southeast-7
**Naming**: `{stack-name}-ecr-state-stack`
**Skip for**: Lambda services (authen, cron) and EC2 services (proxy)

```bash
aws cloudformation create-stack \
  --stack-name excise-wine-nodejs-api-staging-ecr-state-stack \
  --template-body file://ci/cloudformation-ecr-state.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=excise \
    ParameterKey=Project,ParameterValue=wine \
    ParameterKey=Module,ParameterValue=nodejs-api-staging \
  --region ap-southeast-7
```

### 3. Build Stack (apse1)
**Template**: `ci/cloudformation-build-github.yaml`
**Purpose**: Creates CodeBuild project + GitHub webhook for CI
**Resources**: CodeBuild::Project, IAM::Role, CodeBuild webhook
**Region**: ap-southeast-1 (ALWAYS — CodeBuild runs here)
**Naming**: `{stack-name}-build`

Key parameters:
```
Org, Project, Module, GitHubBranch, GitHubRepoName,
DevOpsModulesRepo=thinkbit-devops-modules, DevOpsModulesBranch=main,
ArtifactBucket=thinkbit-devops-artifacts, ArtifactBucketRegion=ap-southeast-1,
DeployRegion=ap-southeast-7, GitHubTokenSecretName=github-deploy-pat
```

CodeBuild config: amazonlinux2-x86_64-standard:5.0, BUILD_GENERAL1_SMALL
Secondary source: thinkbit-devops-modules (DEVOPS_MODULES) — contains params files

### 4. Deploy Stack (apse1)
**Template**: `cd/cloudformation-deploy.yaml`
**Purpose**: Creates CodeBuild project triggered by S3 EventBridge to deploy CF in apse7
**Resources**: CodeBuild::Project, IAM::Role (PowerUserAccess), EventBridge rule
**Region**: ap-southeast-1
**Naming**: `{stack-name}-deploy`
**Trigger**: `deployment-package.zip` uploaded to `s3://thinkbit-devops-artifacts/devops/template/{stack-name}/`

Key parameters:
```
Org, Project, Module, DeployRegion=ap-southeast-7,
DeploymentEnvironment=staging, ArtifactBucket=thinkbit-devops-artifacts
```

Deploy buildspec (inline): reads `{stack-name}-template.yaml` + `{stack-name}-params.json` from zip, runs `aws cloudformation create-stack` or `update-stack` in apse7.

### 5. Service Stack (apse7)
**Template**: Per-service template in `template/{stack-name}-template.yaml`
**Purpose**: The actual infrastructure (ECS, Lambda, ALB, etc.)
**Region**: ap-southeast-7
**Naming**: `{stack-name}` (no suffix)
**Created by**: Deploy CodeBuild project (step 4), NOT manually

---

## Complete Stack Inventory

### ap-southeast-1 (Singapore) — CI/CD Stacks

| Stack Name | Type | Status | Created |
|-----------|------|--------|---------|
| excise-wine-authen-staging-state-stack | State | CREATE_COMPLETE | 2026-03-25 |
| excise-wine-authen-staging-build | Build | UPDATE_COMPLETE | 2026-03-25 |
| excise-wine-authen-staging-deploy | Deploy | CREATE_COMPLETE | 2026-03-25 |
| excise-wine-nodejs-api-staging-build | Build | UPDATE_COMPLETE | 2025-12-16 |
| excise-wine-nodejs-api-staging-deploy | Deploy | UPDATE_COMPLETE | 2025-12-16 |
| excise-wine-go-api-staging-build | Build | CREATE_COMPLETE | 2025-12-16 |
| excise-wine-go-api-staging-deploy | Deploy | UPDATE_COMPLETE | 2025-12-16 |
| excise-wine-nodejs-api-build | Build | CREATE_COMPLETE | 2026-02-20 |
| excise-wine-nodejs-api-deploy | Deploy | CREATE_COMPLETE | 2026-02-20 |
| excise-wine-go-api-build | Build | CREATE_COMPLETE | 2026-02-20 |
| excise-wine-go-api-deploy | Deploy | CREATE_COMPLETE | 2026-02-20 |
| excise-wine-proxy-build | Build | CREATE_COMPLETE | 2025-12-22 |
| excise-wine-proxy-deploy | Deploy | UPDATE_COMPLETE | 2025-12-22 |
| excise-car-backend-staging-build | Build | CREATE_COMPLETE | 2026-04-07 |
| excise-car-backend-staging-deploy | Deploy | UPDATE_COMPLETE | 2026-04-07 |
| excise-car-backend-state-stack | State | CREATE_COMPLETE | 2026-01-14 |
| excise-car-cron-build | Build | CREATE_COMPLETE | 2026-02-02 |
| excise-car-cron-deploy | Deploy | UPDATE_COMPLETE | 2026-02-02 |
| excise-car-cron-state-stack | State | CREATE_COMPLETE | 2026-02-02 |
| thinkbit-devops-release-state-stack | State | CREATE_COMPLETE | 2026-03-31 |
| thinkbit-devops-jenkins-build | Build | UPDATE_COMPLETE | 2026-03-23 |
| thinkbit-devops-jenkins-deploy | Deploy | UPDATE_COMPLETE | 2026-03-24 |
| thinkbit-devops-material-stack | State | CREATE_COMPLETE | 2025-12-11 |
| thinkbit-devops-iac | Infra | CREATE_COMPLETE | 2025-12-08 |
| excise-oil-ocr-staging-build | Build | UPDATE_COMPLETE | 2025-12-04 |
| excise-oil-ocr-staging-deploy | Deploy | UPDATE_COMPLETE | 2025-12-07 |
| excise-oil-ocr-staging-stack | State | CREATE_COMPLETE | 2025-12-04 |
| excise-oil-ocr-staging-ecr-stack | ECR | CREATE_COMPLETE | 2025-12-04 |
| excise-wine-python-api-staging-build | Build (DEPRECATED) | CREATE_COMPLETE | 2025-12-16 |
| excise-wine-python-api-staging-deploy | Deploy (DEPRECATED) | UPDATE_COMPLETE | 2025-12-16 |

### ap-southeast-7 (Bangkok) — Service + State Stacks

| Stack Name | Type | Status | Last Updated |
|-----------|------|--------|-------------|
| excise-wine-authen-staging | Lambda (Cognito) | UPDATE_COMPLETE | 2026-04-15 |
| excise-wine-authen | Lambda (Cognito) prod | CREATE_COMPLETE | 2026-04-15 |
| excise-wine-nodejs-api-staging | ECS Fargate | UPDATE_COMPLETE | 2026-04-10 |
| excise-wine-nodejs-api | ECS Fargate prod | UPDATE_COMPLETE | 2026-04-05 |
| excise-wine-go-api-staging | ECS Fargate | UPDATE_COMPLETE | 2026-04-15 |
| excise-wine-go-api | ECS Fargate prod | UPDATE_COMPLETE | 2026-03-31 |
| excise-wine-proxy | EC2 + CodeDeploy | UPDATE_COMPLETE | 2026-04-15 |
| excise-wine-service-staging | State | CREATE_COMPLETE | 2026-01-12 |
| excise-car-backend-staging | ECS + Lambda | UPDATE_COMPLETE | 2026-04-15 |
| excise-car-cron | Lambda cron | UPDATE_COMPLETE | 2026-04-06 |
| thinkbit-devops-jenkins | EC2 Jenkins | UPDATE_COMPLETE | 2026-04-13 |
| excise-wine-nodejs-api-staging-ecr-state-stack | ECR | CREATE_COMPLETE | 2025-12-16 |
| excise-wine-go-api-staging-ecr-state-stack | ECR | CREATE_COMPLETE | 2025-12-16 |
| excise-wine-nodejs-api-ecr-state-stack | ECR | CREATE_COMPLETE | 2026-02-20 |
| excise-wine-go-api-ecr-state-stack | ECR | CREATE_COMPLETE | 2026-02-20 |
| excise-car-backend-staging-ecr-state-stack | ECR | CREATE_COMPLETE | 2026-04-07 |
| excise-car-backend-staging-state-stack | State | CREATE_COMPLETE | 2026-04-07 |
| excise-wine-proxy-state-stack | State | CREATE_COMPLETE | 2025-12-22 |
| thinkbit-devops-modules-state-stack | State | CREATE_COMPLETE | 2025-12-16 |
| thinkbit-devops-jenkins-state-stack | State | CREATE_COMPLETE | 2026-03-23 |

---

## S3 Artifact Structure (thinkbit-devops-artifacts)

```
thinkbit-devops-artifacts/                          # apse1 (primary)
├── build-cache/{stack-name}/{build-id}             # CodeBuild cache
├── devops/
│   ├── buildspec/                                  # Legacy buildspec templates
│   ├── devops-tools/
│   │   ├── buildspec-template/                     # Universal templates (nodejs, golang, python, ec2, amplify)
│   │   ├── ci-template/                            # CI CF templates (state, build, ECR)
│   │   ├── cd-template/                            # CD CF templates (deploy, deploy-pipeline)
│   │   └── devops-tool-thirdparty/                 # Helper scripts
│   ├── scripts/                                    # Generate-module-files.sh
│   └── template/{stack-name}/                      # Per-service deployment artifacts
│       ├── {stack-name}-template.yaml              # CF template (latest)
│       ├── {stack-name}-params.json                # CF params (latest)
│       ├── {stack-name}-{version}.zip              # Versioned deployment package
│       ├── deployment-package.zip                  # TRIGGER — upload = deploy
│       ├── build-info.json                         # {version, commit, deployType, timestamp}
│       ├── latest-version.txt                      # Current version string
│       └── {version}/                              # Versioned templates (authen only)
│           └── {stack-name}-template-{version}.yaml
├── lambda/{stack-name}/
│   ├── {version}/{stack-name}-lambda-{version}.zip # Versioned Lambda zip
│   ├── {version}/{stack-name}-lambda-{version}.zip.sha256  # Checksum (authen)
│   └── latest/{stack-name}-lambda.zip              # Latest Lambda zip
├── grafana/                                        # Grafana dashboard JSONs
├── sceptre/prod/prod/{stack-name}/                 # Sceptre deploy history
└── thinkbit/{stack-name}/                          # NEW standard path (parallel to devops/template)
    ├── {version}/
    │   ├── {stack-name}-{version}.zip
    │   ├── {stack-name}-{version}.sha256
    │   ├── {stack-name}-params.json
    │   └── {stack-name}-template-{version}.yaml
    ├── deployment-package.zip
    ├── build-info.json
    └── latest-version.txt

thinkbit-devops-artifacts-apse7/                    # apse7 (cross-region sync)
├── devops/template/{stack-name}/                   # Synced templates + params
├── lambda/{stack-name}/{version}/                  # Synced Lambda zips
├── grafana/                                        # Synced dashboards
├── config/                                         # Monitoring configs (prometheus, yace, mimir)
├── scripts/                                        # Utility scripts
└── thinkbit/{stack-name}/{version}/                # NEW standard synced artifacts
```

### Two S3 Path Standards (Legacy vs New)

| Path | Status | Used By |
|------|--------|---------|
| `devops/template/{stack-name}/` | Legacy (active) | All current buildspecs |
| `thinkbit/{stack-name}/{version}/` | New standard | nodejs-api-staging, go-api-staging, car-backend-staging (in addition to legacy) |

Both paths are populated by newer buildspecs. Migration to `thinkbit/` only is planned but NOT done.

---

## CI/CD Flow (End-to-End)

```
Developer pushes to GitHub branch
       │
       ▼
GitHub Webhook → CodeBuild (apse1) [{stack-name}-build project]
       │
       ├── Fetch params from thinkbit-devops-modules/modules/{stack-name}/
       ├── Version bump via SSM (VERSION_BUILDNUMBER_PARAM)
       │
       ├── DevSecOps Pipeline:
       │   ├── gitleaks (secret scan, warn)
       │   ├── semgrep (SAST, warn — resilient on AL2)
       │   ├── cfn-lint (IaC lint, warn)
       │   ├── npm audit --audit-level=critical (block)
       │   ├── lint + format check (block)
       │   └── tests (block)
       │
       ├── Build (language-specific):
       │   ├── Lambda: npm ci → build → zip dist/ with prod deps
       │   ├── ECS: docker build → push to ECR (apse7)
       │   └── EC2: package nginx config + scripts
       │
       ├── Upload artifacts to S3 (apse1):
       │   ├── lambda/{stack-name}/{version}/ + latest/
       │   ├── devops/template/{stack-name}/ (template, params, versioned zip)
       │   └── thinkbit/{stack-name}/{version}/ (new standard, some services)
       │
       ├── Cross-region sync to apse7 (FATAL on failure):
       │   ├── Lambda zips → thinkbit-devops-artifacts-apse7
       │   └── Templates + params → apse7 bucket
       │
       └── Upload deployment-package.zip LAST (trigger)
              │
              ▼
       EventBridge (S3 PutObject) → CodeBuild (apse1) [{stack-name}-deploy project]
              │
              ├── Unzip deployment-package.zip
              ├── Read {stack-name}-template.yaml + {stack-name}-params.json
              ├── Handle ROLLBACK_COMPLETE (delete + recreate)
              └── aws cloudformation create-stack/update-stack --region ap-southeast-7
                     │
                     ▼
              CloudFormation Stack in apse7 (service infrastructure)
```

---

## Template Locations in Codebase

### CI/CD Templates (reusable)
```
~/develope/DevOps/thinkbit-devops-material/
├── ci/
│   ├── cloudformation-state-github.yaml        # Step 1: GitHub repo + SSM
│   ├── cloudformation-state.yaml               # Step 1 alt: without GitHub
│   ├── cloudformation-ecr-state.yaml           # Step 2: ECR repo
│   ├── cloudformation-build-github.yaml        # Step 3: CodeBuild CI
│   ├── cloudformation-build-github-pipeline.yaml # Step 3 alt: with CodePipeline
│   └── cloudformation-build.yaml               # Step 3 alt: without GitHub
└── cd/
    ├── cloudformation-deploy.yaml              # Step 4: CodeBuild CD
    └── cloudformation-deploy-pipeline.yaml     # Step 4 alt: with CodePipeline
```

### Service Templates (per-project)
```
~/develope/Excise/Wine/excise-wine-authen/template/
├── excise-wine-authen-staging-buildspec.yaml   # CI buildspec
└── excise-wine-authen-staging-template.yaml    # CF template (Lambda + Cognito)

~/develope/Excise/Wine/excise-wine-nodejs-api/template/
├── excise-wine-nodejs-api-staging-buildspec.yaml
└── excise-wine-nodejs-api-staging-template.yaml  # CF template (ECS + ALB + Redis)

~/develope/Excise/Wine/excise-wine-go-api/template/
├── excise-wine-go-api-staging-buildspec.yaml
└── excise-wine-go-api-staging-template.yaml      # CF template (ECS + ALB)

~/develope/Excise/Wine/excise-wine-proxy/template/
├── excise-wine-proxy-buildspec.yaml
└── excise-wine-proxy-template.yaml               # CF template (EC2 + CodeDeploy)

~/develope/Excise/Car/excise-car-backend/template/
├── excise-car-backend-staging-buildspec.yaml
└── excise-car-backend-staging-template.yaml      # CF template (ECS + Lambda cron)

~/develope/Excise/Car/excise-car-cron/template/
├── excise-car-cron-staging-buildspec.yaml
└── excise-car-cron-staging-template.yaml         # CF template (Lambda cron)
```

### Parameter Files (thinkbit-devops-modules)
```
~/develope/DevOps/thinkbit-devops-modules/modules/
├── excise-wine-authen-staging/excise-wine-authen-staging-params.json
├── excise-wine-nodejs-api-staging/excise-wine-nodejs-api-staging-params.json
├── excise-wine-nodejs-api/excise-wine-nodejs-api-params.json
├── excise-wine-go-api-staging/excise-wine-go-api-staging-params.json
├── excise-wine-go-api/excise-wine-go-api-params.json
├── excise-wine-proxy/excise-wine-proxy-params.json
├── excise-car-backend-staging/excise-car-backend-staging-params.json
├── excise-car-cron-staging/excise-car-cron-staging-params.json
├── excise-car-cron/excise-car-cron-params.json
└── thinkbit-devops-jenkins/thinkbit-devops-jenkins-params.json
```

---

## How to Create a New Service (Step-by-Step)

### Example: Creating `excise-wine-report-staging` (Lambda Node.js)

```bash
# Variables
ORG=excise
PROJECT=wine
MODULE=report-staging
STACK_NAME=${ORG}-${PROJECT}-${MODULE}

# Step 1: State Stack (creates GitHub repo)
aws cloudformation create-stack \
  --stack-name ${STACK_NAME}-state-stack \
  --template-body file://ci/cloudformation-state-github.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=$ORG \
    ParameterKey=Project,ParameterValue=$PROJECT \
    ParameterKey=Module,ParameterValue=$MODULE \
    ParameterKey=GitHubBranch,ParameterValue=staging \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region ap-southeast-1

aws cloudformation wait stack-create-complete \
  --stack-name ${STACK_NAME}-state-stack --region ap-southeast-1

# Step 2: ECR Stack (SKIP for Lambda, only for ECS)
# aws cloudformation create-stack --stack-name ${STACK_NAME}-ecr-state-stack ...

# Step 3: Build Stack (CodeBuild CI)
aws cloudformation create-stack \
  --stack-name ${STACK_NAME}-build \
  --template-body file://ci/cloudformation-build-github.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=$ORG \
    ParameterKey=Project,ParameterValue=$PROJECT \
    ParameterKey=Module,ParameterValue=$MODULE \
    ParameterKey=GitHubBranch,ParameterValue=staging \
    ParameterKey=GitHubRepoName,ParameterValue=${ORG}-${PROJECT}-report \
    ParameterKey=DeployRegion,ParameterValue=ap-southeast-7 \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region ap-southeast-1

aws cloudformation wait stack-create-complete \
  --stack-name ${STACK_NAME}-build --region ap-southeast-1

# Step 4: Deploy Stack (CodeBuild CD)
aws cloudformation create-stack \
  --stack-name ${STACK_NAME}-deploy \
  --template-body file://cd/cloudformation-deploy.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=$ORG \
    ParameterKey=Project,ParameterValue=$PROJECT \
    ParameterKey=Module,ParameterValue=$MODULE \
    ParameterKey=DeployRegion,ParameterValue=ap-southeast-7 \
    ParameterKey=DeploymentEnvironment,ParameterValue=staging \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region ap-southeast-1

aws cloudformation wait stack-create-complete \
  --stack-name ${STACK_NAME}-deploy --region ap-southeast-1

# Step 5: Create params file in thinkbit-devops-modules
mkdir -p ~/develope/DevOps/thinkbit-devops-modules/modules/${STACK_NAME}
# Create ${STACK_NAME}-params.json with CF parameters

# Step 6: Create buildspec + service template in project repo
# template/${STACK_NAME}-buildspec.yaml (copy from authen as reference)
# template/${STACK_NAME}-template.yaml (service-specific CF template)

# Step 7: Push code → webhook triggers build → build uploads to S3 → deploy triggers
```

### For ECS Service: Add Step 2 (ECR) before Step 3

```bash
aws cloudformation create-stack \
  --stack-name ${STACK_NAME}-ecr-state-stack \
  --template-body file://ci/cloudformation-ecr-state.yaml \
  --parameters \
    ParameterKey=Org,ParameterValue=$ORG \
    ParameterKey=Project,ParameterValue=$PROJECT \
    ParameterKey=Module,ParameterValue=$MODULE \
  --region ap-southeast-7

aws cloudformation wait stack-create-complete \
  --stack-name ${STACK_NAME}-ecr-state-stack --region ap-southeast-7
```

---

## Service-Specific Stack Outputs (apse7)

### excise-wine-authen-staging (Lambda + Cognito)
- UserPoolId: ap-southeast-7_3Pj25ydGY
- WebClientId: 3latneo7ps6nfl1objj3nba7qv (from PORTABLE_CONTEXT)
- MobileClientId: 2iuq84fct93gbscv0n5f1mlp6e
- BackendClientId: 5s9t781gqunn5f3bhcm11pdb0i
- IdentityPoolId: ap-southeast-7:1ae73862-360a-4f58-8c31-b26c4becced7
- Domain: https://excise-wine-authen-staging.auth.ap-southeast-7.amazoncognito.com

### excise-wine-nodejs-api-staging (ECS + ALB + Redis)
- Cluster: excise-wine-nodejs-api-staging-cluster
- ALB: wine-nodejs-staging-alb-1750877297.ap-southeast-7.elb.amazonaws.com
- URL: https://excise-wine-nodejs-api-staging.devthinkbit.com
- Redis: excise-wine-nodejs-api-staging-redis:6379

### excise-wine-go-api-staging (ECS + ALB)
- ALB: wine-go-staging-alb-618271064.ap-southeast-7.elb.amazonaws.com
- URL: https://excise-wine-go-api-staging.devthinkbit.com
- NOTE: Uses nodejs-api cluster (shared, cost saving)

### excise-wine-proxy (EC2 + CodeDeploy + Nginx)
- Instance: i-0e4fd71aa0d6d868a
- PublicIP: 43.209.238.131
- SSH: ssh -i thinkbit-key.pem ec2-user@43.209.238.131
- CodeDeploy: excise-wine-proxy / excise-wine-proxy-dg

### excise-car-backend-staging (ECS + Lambda cron)
- Cluster: excise-car-backend-staging-cluster
- ALB: excise-car-backend-staging-alb-1061569315.ap-southeast-7.elb.amazonaws.com
- Lambda: autoRejection, getCurrency

### excise-car-cron (Lambda)
- Lambda: autoRejection, getCurrency

---

## Service Architecture Details

### Deploy Types by Service

| Service | Deploy Type | Container | Runtime | Port | CPU/Mem |
|---------|-----------|-----------|---------|------|---------|
| wine-authen | Lambda | — | Node.js 20 | — | 256MB/30s |
| wine-nodejs-api | ECS EC2 | Docker | Node.js 20 | 3000 | 1024/2048 |
| wine-go-api | ECS EC2 (shared cluster) | Docker | Go 1.21 | 8080 | 256/512 |
| wine-proxy | EC2 CodeDeploy | — | Nginx (AL2023 ARM64) | 80/443 | c7g.large |
| car-backend | ECS Fargate + Lambda | Docker + zip | Node.js 20 | 3000 | 256/512 |
| car-cron | Lambda | — | Node.js 20 | — | — |

### Shared Infrastructure Pattern
- **wine-nodejs-api** creates the shared ECS cluster, ALB, and ElastiCache Redis
- **wine-go-api** imports cluster/roles via CF exports: `!ImportValue "excise-wine-staging-ClusterArn"`
- Export naming: `${AWS::StackName}-ResourceName`

### Security Features per Template
- **WAF**: ALB-fronted services have WAFv2 with rate limiting (2000 req/min)
- **Secrets**: Runtime injection via Secrets Manager → ECS Task Definition (NEVER in Docker)
- **VPC**: Lambda + ECS tasks deployed in VPC with security groups
- **ElastiCache**: Redis 7.1 (cache.t3.micro) for nodejs-api

### EventBridge Scheduler (car-backend/cron)
- AutoRejection: `cron(0 1 * * ? *)` — 01:00 Asia/Bangkok
- GetCurrency: `cron(0 0 * * ? *)` — 00:00 Asia/Bangkok
- Uses EventBridge Scheduler (not deprecated CloudWatch Events)

### DevSecOps Scan Matrix

| Scan | Node.js | Go | Python | EC2 |
|------|---------|-----|--------|-----|
| gitleaks | ✅ | ✅ | ✅ | ✅ |
| semgrep | ✅ (warn) | ✅ (warn) | ✅ (block ERROR) | — |
| npm audit | ✅ (block critical) | — | — | — |
| govulncheck | — | ✅ (block) | — | — |
| cfn-lint | ✅ (warn) | ✅ (warn) | ✅ (warn) | ✅ (warn) |
| ESLint | ✅ (block) | — | — | — |
| golangci-lint | — | ✅ (warn) | — | — |

### Params Fetch Priority
1. `thinkbit-devops-modules/modules/{STACK_NAME}/{STACK_NAME}-params.json` (secondary source)
2. `s3://{ARTIFACT_BUCKET}/devops/template/{STACK_NAME}/{STACK_NAME}-params.json` (fallback)
3. Empty `[]` (default)

---

## CodeCommit vs GitHub Source Variants

The CI/CD templates come in two flavors:

| Template | Source | Trigger |
|----------|--------|---------|
| `cloudformation-state.yaml` | CodeCommit | EventBridge (referenceCreated/Updated) |
| `cloudformation-state-github.yaml` | GitHub (THINKBITTH org) | Lambda custom resource creates repo |
| `cloudformation-build.yaml` | CodeCommit + CodePipeline | CodePipeline orchestration |
| `cloudformation-build-github.yaml` | GitHub + CodeBuild webhook | GitHub webhook direct |
| `cloudformation-deploy.yaml` | S3 EventBridge | deployment-package.zip upload |
| `cloudformation-deploy-pipeline.yaml` | S3 + CodePipeline | CodePipeline orchestration |

**Current services use GitHub variants** (cloudformation-*-github.yaml).
CodeCommit variants exist for services that don't need GitHub.

### SSM Parameters Created Per Service
```
/devops/{stack-name}/version-buildnumber    # Auto-incremented build number
/devops/{stack-name}/github-token-secret    # GitHub PAT secret name
/devops/{stack-name}/github-repo            # owner/repo
/devops/{stack-name}/github-branch          # tracked branch
```

### CodeBuild Environment Variables (auto-set by templates)
```
AWS_DEFAULT_REGION          ap-southeast-1
AWS_ACCOUNT_ID              498952158610
ORG                         excise
PROJECT                     wine
MODULE                      authen-staging
STACK_NAME                  excise-wine-authen-staging
ARTIFACT_BUCKET             thinkbit-devops-artifacts
VERSION_BUILDNUMBER_PARAM   /devops/{stack-name}/version-buildnumber
BUILD_REGION                ap-southeast-1
DEPLOY_REGION               ap-southeast-7
```

### Universal Buildspec Templates (for new services)
Located in `thinkbit-devops-material/buildspec/`:
- `buildspec-nodejs.yaml` — Lambda, ECS, EC2, Beanstalk, EKS, AppRunner, Lightsail
- `buildspec-golang.yaml` — same 7 deploy types
- `buildspec-python.yaml` — same 7 deploy types
- `buildspec-ec2.yaml` — EC2/CodeDeploy/Nginx/static
- `buildspec-amplify.yaml` — Amplify/frontend

Note: Current services use **project-specific buildspecs** in each repo's `template/` dir.
Universal templates are for new projects only.

### thinkbit-devops-modules Structure
```
~/develope/DevOps/thinkbit-devops-modules/modules/
├── {stack-name}/
│   ├── {stack-name}-params.json        # CF parameters
│   ├── {stack-name}-buildspec.yaml     # Buildspec (older services)
│   └── {stack-name}-template.yaml      # CF template (older services)
```
Note: Newer services (authen) keep buildspec + template in the **project repo** `template/` dir,
while params remain in devops-modules. Older services may have all 3 in devops-modules.

### GitHub Actions Alternative (thinkbit-devops-pipeline)
Located in `~/develope/DevOps/thinkbit-devops-pipeline/.github/workflows/`:
- `build-template.yml`, `quality-template.yml`, `security-template.yml`, `notify-template.yml`
- `ci.yml` (main CI workflow)
Not actively used by Excise services — all use CodeBuild.


---

**Graph**: [[../Documents/Obsidian Vault/AI-Hub/patterns/MOC|🧭 Graph Hub]] · [[MEMORY|Memory Index]] · [[knowledge_index|Pattern Index]] · [[lessons_learned|Lessons]]
