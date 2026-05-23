# Terraform Infrastructure — Complete Reference

> All Terraform configs across AWS, GCP, Azure. Updated: 2026-04-16

---

## Terraform Projects Overview

| Project | Cloud | Purpose | State Backend | Workspaces |
|---------|-------|---------|--------------|------------|
| thinkbit-devops-iac | AWS | Core infra (RDS, EC2, VPC, Lambda) | S3 `thinkbit-terraform-state` apse1 | dev, staging, prod |
| thinkbit-devops-cloudvpn | AWS | Pritunl VPN server | Local/S3 | single |
| thinkbit-devops-SwithRole | AWS | Multi-account IAM org | S3 | single |
| AWS-Fix-Assassment | AWS | Security remediation | S3 | production |
| gcp-terraform | GCP | GCP projects, VMs, SQL | GCS | staging, prod |
| azure-terraform/mfa-portal | Azure | MFA portal App Service | Local | single |
| Rollback-script/gke-backup | GCP | GKE backup/restore | Local | single |

---

## AWS Terraform (thinkbit-devops-iac)

### Backend
```hcl
backend "s3" {
  bucket         = "thinkbit-terraform-state"
  key            = "rds-proxy/terraform.tfstate"
  region         = "ap-southeast-1"
  dynamodb_table = "terraform-locks"  # state locking
}
```

### Provider
```hcl
provider "aws" {
  region = "ap-southeast-7"  # Bangkok (deploy region)
}
```

### Resources Managed (main.tf ~450 lines)

**Imported (existing) resources:**
- VPC: `vpc-0b10104e1ed5ae1f7`
- Subnets: `subnet-02f5c447316f196e1`, `subnet-09b0b44d8a0a07245`
- Security Groups
- EC2 Instances (t3.large/xlarge across AZs)
- RDS Instances (SQL Server, db.m6i.2xlarge, 300GB gp3)
- Application Load Balancers
- Lambda Functions
- DynamoDB Tables
- SNS Topics

**Created resources:**
- RDS Proxy (with Secrets Manager integration)

### Modules
```
modules/
├── rds-proxy/          # RDS Proxy creation with credentials rotation
├── security-groups/    # Security group management
├── s3/                 # S3 bucket configuration
├── sns/                # SNS topic setup
├── alb/                # Load balancer management
└── ...
```

### Environments
```
environments/
├── dev/terraform.tfvars
├── staging/terraform.tfvars
└── prod/terraform.tfvars
```

### Key Variables
- `region`: ap-southeast-7
- `vpc_id`: vpc-0b10104e1ed5ae1f7
- `rds_instance_identifier`: think-bit-rds
- `rds_engine`: sqlserver-se
- `rds_instance_class`: db.m6i.2xlarge
- `rds_storage`: 300 (gp3)

---

## GCP Terraform (gcp-terraform)

### Provider
```hcl
provider "google" {
  version = "~> 5.45.2"
  region  = "asia-southeast1"  # Singapore
}
```

### Main File
`main-new-project.tf` — 5,170 lines covering:
- GCP Projects (creation & configuration)
- Compute Engine VMs
- Cloud SQL instances
- Cloud Storage buckets
- VPC, Subnets, Firewall rules
- Service Accounts & IAM bindings

### Environments
```
environments/
├── prod.tfvars
├── staging.tfvars
└── staging-new-project.tfvars
```

### Plan Files
- `singapore-ohv2.tfplan`
- `singapore-new-vms.tfplan`

---

## Azure Terraform (azure-terraform)

### Provider
```hcl
provider "azurerm" {
  version = "~> 3.117.1"
  features {}
}
```

### Projects

**1. mfa-portal/**
- Azure App Service
- Azure SQL Database
- Azure KeyVault
- Resource Group

**2. tb-ai-mfa-portal-gen2/**
- Full-stack app (Flask/FastAPI + HTML frontend)
- AI/ML pipeline (Azure OpenAI GPT-4)
- Pipeline blocks: summarize, draft, classify, review, OCR, translate, PDF gen, SMS, webhook, booking
- SQLite (demo) / PostgreSQL (production)

---

## VPN Terraform (thinkbit-devops-cloudvpn)

### Architecture
```
VPC (10.0.0.0/16)
├── Public Subnet (10.0.1.0/24)
│   └── EC2 t3.medium + Elastic IP
│       └── Pritunl VPN Server
│           └── MongoDB backend
└── Private Subnet (10.0.2.0/24)
    └── Internal resources
```

### Files
- `main.tf` — VPC, subnets, routing, IGW
- `ec2.tf` — EC2 instance + EIP
- `security_groups.tf` — VPN SG (UDP/TCP 1194) + private access SG
- `variables.tf` — Config params
- `outputs.tf` — Public IP, endpoint URL
- `user_data.sh` — EC2 init (MongoDB + Pritunl install)
- Cost: ~$35-50/month

---

## Multi-Account IAM (thinkbit-devops-SwithRole)

### Modules
```
terraform/modules/
├── organization/      # AWS Organizations setup
├── iam-user/          # Management account user
├── member-account/    # Prod & pre-prod member accounts
├── security/          # CloudTrail, Config, SCPs
└── cost-management/   # Budgets, anomaly detection, alerts
```

### Features
- AWS Organizations with OUs
- SCP-based guardrails
- CloudTrail centralized logging
- Budget alerts (email notifications)
- Cross-account AssumeRole

---

## Security Remediation Terraform (AWS-Fix-Assassment)

### Modules
```
modules/
├── 01-iam-security/    # IAM policy boundaries, password policy
└── 02-s3-security/     # S3 Block Public Access, bucket policies
```

### Environment
```
environments/
├── production/
│   ├── main.tf, variables.tf, backend.tf, outputs.tf, providers.tf
│   ├── terraform.tfvars
│   └── tfplan
└── rollback/
    ├── rollback-phase1-only.tfvars
    ├── rollback-phase2-only.tfvars
    └── rollback-all.tfvars
```

### Current State (Apr 5 scan)
- Total findings: 5,759 (down from 6,683)
- FAIL: 1,964 (down from 2,353, -16.5%)
- 203 unique security checks
- Progress: ~67% complete

### Critical Remaining
1. Root account: 2 active access keys (DELETE)
2. Root MFA: virtual -> needs hardware key
3. 7 IAM users with AdministratorAccess
4. 85 ECS task defs with env var secrets
5. 9 Lambda functions with hardcoded secrets
6. 2 public S3 buckets
7. Account-level S3 Block Public Access not enabled

---

## Common Terraform Commands

```bash
# Initialize
terraform init

# Select workspace
terraform workspace select staging

# Plan (ALWAYS before apply)
terraform plan -var-file=environments/staging/terraform.tfvars

# Apply
terraform apply -var-file=environments/staging/terraform.tfvars

# Import existing resource
terraform import aws_instance.example i-1234567890abcdef0

# Destroy (CAUTION)
terraform destroy -var-file=environments/staging/terraform.tfvars
```

---

## State File Locations

| Project | Backend | Bucket/Path |
|---------|---------|-------------|
| thinkbit-devops-iac | S3 | `thinkbit-terraform-state/rds-proxy/terraform.tfstate` |
| gcp-terraform | GCS | (check backend-new-project.tf) |
| azure-terraform | Local | `terraform.tfstate` in project dir |
| AWS-Fix-Assassment | S3 | (check environments/production/backend.tf) |
