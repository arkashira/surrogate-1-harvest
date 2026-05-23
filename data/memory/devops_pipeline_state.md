---
name: DevOps Pipeline State
description: Current state of all CI/CD pipelines, S3 structure, repo conventions, known issues, and lessons learned
type: project
updated: 2026-04-15
---

## AWS Account
- Account ID: 498952158610
- Build Region: ap-southeast-1 (Singapore)
- Deploy Region: ap-southeast-7 (Bangkok/Thailand)

## S3 Buckets
- Build artifacts (apse1): `thinkbit-devops-artifacts`
- Deploy artifacts (apse7): `thinkbit-devops-artifacts-apse7`

## Standard S3 Structure
```
thinkbit-devops-artifacts/
├── devops/devops-tools/
│   ├── buildspec-template/    # Universal buildspec templates (nodejs, golang, python, ec2)
│   ├── ci-template/           # CI CloudFormation templates
│   ├── cd-template/           # CD CloudFormation templates
│   └── devops-tool-thirdparty/
├── lambda/{stack-name}/{version}/  # Lambda zips + checksums
├── thinkbit/{stack-name}/{version}/ # ECS/EC2 templates + params + Docker tags
└── devops/template/{stack-name}/    # Legacy path (still in use by current buildspecs)
```

## Standard Prefix: `thinkbit/` (NOT `devops/template/`)
Note: Current buildspecs still upload to `devops/template/` — migration to `thinkbit/` is planned but NOT done yet.

## Services Inventory

### Wine Project (excise-wine-*)
| Service | Type | Branch | Trigger | Deploy | Status |
|---------|------|--------|---------|--------|--------|
| excise-wine-authen-staging | Lambda (Node.js 20) | staging | webhook | Lambda in apse7 | ✅ Working |
| excise-wine-go-api-staging | ECS (Docker Go 1.21) | staging/aws | webhook | ECS Fargate in apse7 | ✅ Working |
| excise-wine-nodejs-api-staging | ECS (Docker Node.js 20) | staging/aws | webhook | ECS Fargate in apse7 | ✅ Working |
| excise-wine-proxy | EC2 (CodeDeploy Nginx) | main | webhook | EC2 in apse7 | ✅ Working |

### Car Project (excise-car-*)
| Service | Type | Branch | Trigger | Deploy | Status |
|---------|------|--------|---------|--------|--------|
| excise-car-backend-staging | ECS + Lambda cron | staging | webhook | ECS + Lambda in apse7 | ✅ Working |
| excise-car-cron | Lambda (Node.js 20) | main | webhook | Lambda in apse7 | ✅ Working |

### DEPRECATED: excise-wine-python-api — REMOVED from S3, not in use

## Conventions
- Buildspec location: `template/{stack-name}-buildspec.yaml` (NOT root)
- Params format: CloudFormation array `[{"ParameterKey":"","ParameterValue":""}]`
- Versioning: Build number via SSM `VERSION_BUILDNUMBER_PARAM`
- Cross-region sync: FATAL on failure (never non-fatal)
- Secrets: NEVER bake into Docker images. Inject at runtime via ECS Task Definition
- Shared cluster: go-api uses nodejs-api cluster intentionally (cost saving)
- Deploy trigger: `deployment-package.zip` upload → EventBridge → CodeBuild deploy → CF update

## DevSecOps Pipeline Standard (all services now have):
```
gitleaks (warn) → semgrep (warn, resilient) → cfn-lint (warn) → SCA (block critical) → lint (warn) → test (warn) → build → checksum → build-info.json → cross-region sync (fatal) → deploy trigger
```

## CodeBuild Projects (ap-southeast-1)
All use: amazonlinux2-x86_64-standard:5.0, BUILD_GENERAL1_SMALL
Secondary source: thinkbit-devops-modules (DEVOPS_MODULES)

## Universal Buildspec Templates (thinkbit-devops-material)
Located in: `devops/devops-tools/buildspec-template/`
- `buildspec-nodejs.yaml` — Lambda, ECS, EC2, Beanstalk, EKS, AppRunner, Lightsail
- `buildspec-golang.yaml` — Same 7 deploy types
- `buildspec-python.yaml` — Same 7 deploy types
- `buildspec-ec2.yaml` — EC2/CodeDeploy/Nginx/static
Note: Current services use project-specific buildspecs, NOT universal templates. Templates are for new projects.

## Repos & Locations
```
~/develope/Excise/Wine/excise-wine-authen/      # Lambda Node.js auth (Cognito)
~/develope/Excise/Wine/excise-wine-go-api/       # ECS Go API
~/develope/Excise/Wine/excise-wine-nodejs-api/   # ECS Node.js API
~/develope/Excise/Wine/excise-wine-proxy/        # EC2 Nginx proxy
~/develope/Excise/Car/excise-car-backend/        # ECS + Lambda cron
~/develope/Excise/Car/excise-car-cron/           # Lambda cron
~/develope/DevOps/thinkbit-devops-material/      # Universal templates + CI/CD templates
~/develope/DevOps/thinkbit-devops-modules/       # CodeBuild secondary source (params, modules)
```

## Known Issues & Gotchas

### semgrep on Amazon Linux 2 CodeBuild
- `pip3 install semgrep` installs but `semgrep-core` binary often missing/incompatible
- MUST check `semgrep --version` works before running scan
- Pattern: `if command -v semgrep && semgrep --version; then scan; else warn; fi`
- NEVER make semgrep a hard blocker without testing it runs on the platform first

### npm audit in CodeBuild
- `--omit=dev` excludes dev dependencies (reduces false positives)
- `--audit-level=critical` only blocks on critical severity
- Transitive dependency vulns (firebase-admin chain) may need `npm audit fix` in the repo
- `npm audit fix` (without --force) = safe, non-breaking fixes only

### Cross-region S3 sync
- IAM roles need explicit permission to apse7 bucket (`thinkbit-devops-artifacts-apse7`)
- go-api and nodejs-api CodeBuild roles had inline policy `S3CrossRegionTH` added OUT OF BAND (not in CF template)
- If CI stacks are recreated, this policy MUST be added to the CloudFormation template

### Lambda zip creation (authen)
- `npm ci` in `dist/` requires BOTH `package.json` AND `package-lock.json` copied
- Missing package-lock.json = silent failure (npm ci fails, breaks && chain, zip never created)
- Always add guard check: `if [ ! -f "file.zip" ]; then exit 1; fi`

### Docker secrets
- nodejs-api and car-backend previously baked secrets into Docker images via build args
- FIXED: now use runtime injection via ECS Task Definition + Secrets Manager
- Pattern: Docker gets placeholder → ECS injects real values at runtime

## Changes Made (2026-04-15 Session)

### Security Fixes (CRITICAL)
1. Removed secrets baking from Docker images (nodejs-api, car-backend)
2. Added DevSecOps pipeline to all 4 active services
3. Made cross-region sync FATAL across all buildspecs
4. Fixed npm audit critical vulns in car-backend (basic-ftp, fast-xml-parser)

### Bug Fixes
1. Fixed Lambda zip creation (package-lock.json not copied to dist/)
2. Changed authen CodeBuild webhook: main → staging branch
3. Added IAM S3 cross-region permissions to ECS build roles
4. Made semgrep installation resilient on Amazon Linux 2

### Infrastructure
1. S3 cleanup: removed old versions, deprecated python-api, malformed paths (~4GB freed)
2. Created universal buildspec templates supporting 7 deploy types
3. Uploaded templates to S3

### Cognito Migration (from earlier session, 2026-03-25)
- Firebase → Cognito for excise-wine-authen
- User Pool: ap-southeast-7_3Pj25ydGY
- Lambda User Migration Trigger with dual Firebase API key support
- SES: no-reply@devthinkbit.com (DKIM verified, Thai templates)
- feature/cognito branches exist in all repos (not yet merged to staging)


---

**Graph**: [[../Documents/Obsidian Vault/AI-Hub/patterns/MOC|🧭 Graph Hub]] · [[MEMORY|Memory Index]] · [[knowledge_index|Pattern Index]] · [[lessons_learned|Lessons]]
