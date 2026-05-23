# DevOps / SRE / Cloud / Security / Platform Engineering — MEGA Reference

> Ultimate ops reference loaded into every AI session. Written for senior engineers.
> Covers: AWS (deep), GCP, Azure, Huawei Cloud, IaC, Containers, CI/CD, Security, SRE, Platform Eng, Observability, Networking, DBA, Linux, Cost Engineering.
> Updated: 2026-04-16

---

# Table of Contents

1. [AWS (Primary Cloud — Deep)](#1-aws-primary-cloud)
2. [GCP](#2-gcp)
3. [Azure](#3-azure)
4. [Huawei Cloud](#4-huawei-cloud)
5. [Infrastructure as Code](#5-infrastructure-as-code)
6. [Containers & Orchestration](#6-containers--orchestration)
7. [CI/CD Pipelines](#7-cicd-pipelines)
8. [DevSecOps & Security](#8-devsecops--security)
9. [SRE Practices](#9-sre-practices)
10. [Platform Engineering](#10-platform-engineering)
11. [Observability](#11-observability)
12. [Networking](#12-networking)
13. [Database Administration](#13-database-administration)
14. [Linux & Shell](#14-linux--shell)
15. [Cost Engineering](#15-cost-engineering)

---

# 1. AWS (Primary Cloud)

## Account & Regions

| Key | Value |
|-----|-------|
| Account ID | 498952158610 |
| Build Region | ap-southeast-1 (Singapore) — CodeBuild, CodePipeline |
| Deploy Region | ap-southeast-7 (Bangkok) — ECS, Lambda, CloudFormation |
| S3 Build Artifacts | `thinkbit-devops-artifacts` (apse1) |
| S3 Deploy Artifacts | `thinkbit-devops-artifacts-apse7` (apse7) |
| GitHub Org | THINKBITTH |
| Terraform State | `thinkbit-terraform-state` (apse1) + DynamoDB `terraform-locks` |

Cross-region pattern: build in apse1 -> sync to apse7 -> deploy in apse7. Sync is FATAL on failure.

## 1.1 Compute

### EC2

**Instance Selection Decision Tree:**
```
Need GPU?          -> P/G instances (p5.48xlarge for training, g5.xlarge for inference)
Need ARM (cost)?   -> Graviton (c7g, m7g, r7g) — 20-40% cheaper, better perf/watt
Need burst?        -> t3/t4g (burstable, check CPU credits)
Compute-heavy?     -> c7i/c7g (compute optimized)
Memory-heavy?      -> r7i/r7g (memory optimized), x2idn for extreme
Storage IOPS?      -> i4i (storage optimized, local NVMe)
General purpose?   -> m7i/m7g (balanced)
```

**Placement Groups:**

| Type | Use Case | Constraint |
|------|----------|------------|
| Cluster | HPC, low-latency | Same AZ, same instance type ideal |
| Spread | HA, max 7 per AZ | Different racks, up to 7 instances/AZ |
| Partition | HDFS, Cassandra, Kafka | Logical partitions, different racks |

**Graviton (ARM64):**
- 20-40% better price-performance vs x86
- Our proxy runs on Graviton: `excise-wine-proxy` on ARM64
- Docker: multi-arch builds with `docker buildx` or platform-specific `--platform linux/arm64`
- Not all AMIs support ARM — check marketplace

**Key Operations:**
```bash
# Find latest AL2023 AMI
aws ec2 describe-images --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023*-arm64" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text

# User data (cloud-init)
#!/bin/bash
set -euo pipefail
yum update -y
# ... bootstrap

# Instance metadata (IMDSv2 required)
TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id
```

**Anti-patterns:**
- Never use IMDSv1 (vulnerable to SSRF). Enforce IMDSv2 with `HttpTokens: required`
- Never hardcode AMI IDs. Use SSM parameters or `data.aws_ami` in Terraform
- Never run as root inside EC2. Use `ec2-user` or dedicated service accounts

### ECS

**Fargate vs EC2 Launch Type:**

| Factor | Fargate | EC2 |
|--------|---------|-----|
| Management | Serverless, no instances | You manage ASG, AMIs, patching |
| Cost | Higher per-vCPU, no commitment | Lower at scale, RIs/SPs apply |
| GPU | Not supported | Supported |
| Startup time | 30-90s (image pull) | Faster if image cached |
| Max resources | 16 vCPU, 120GB RAM | Instance limits |
| Storage | 200GB ephemeral | Instance storage + EBS |
| Best for | Variable workloads, small teams | Steady-state, GPU, cost-sensitive |

**Our ECS Setup:**
- `excise-wine-nodejs-api-staging`: Fargate, Node.js 20
- `excise-wine-go-api-staging`: Fargate, Go 1.21 (shares cluster with nodejs-api for cost)
- `excise-car-backend-staging`: Fargate + Lambda cron

**Task Definition Best Practices:**
```json
{
  "family": "excise-wine-nodejs-api-staging",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "containerDefinitions": [{
    "name": "app",
    "image": "498952158610.dkr.ecr.ap-southeast-7.amazonaws.com/excise-wine-nodejs-api-staging:1.0.42",
    "portMappings": [{"containerPort": 3000, "protocol": "tcp"}],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -f http://localhost:3000/health || exit 1"],
      "interval": 30,
      "timeout": 5,
      "retries": 3,
      "startPeriod": 60
    },
    "secrets": [
      {"name": "DB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:ap-southeast-7:498952158610:secret:excise/wine/staging/db-password"}
    ],
    "environment": [
      {"name": "NODE_ENV", "value": "staging"},
      {"name": "PORT", "value": "3000"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/excise-wine-nodejs-api-staging",
        "awslogs-region": "ap-southeast-7",
        "awslogs-stream-prefix": "ecs"
      }
    }
  }]
}
```

**Service Discovery:**
- AWS Cloud Map for internal service-to-service
- Pattern: `service.namespace.local` DNS records
- ALB for external traffic with target groups

**ECS Exec (debugging):**
```bash
aws ecs execute-command \
  --cluster excise-wine-cluster \
  --task <task-id> \
  --container app \
  --interactive \
  --command "/bin/sh"
```

**Scaling:**
```yaml
# Target tracking — scale on CPU
ScalingTarget:
  Type: AWS::ApplicationAutoScaling::ScalableTarget
  Properties:
    MaxCapacity: 10
    MinCapacity: 2
    ResourceId: !Sub service/${ClusterName}/${ServiceName}
    ScalableDimension: ecs:service:DesiredCount
    ServiceNamespace: ecs

ScalingPolicy:
  Type: AWS::ApplicationAutoScaling::ScalingPolicy
  Properties:
    PolicyType: TargetTrackingScaling
    TargetTrackingScalingPolicyConfiguration:
      PredefinedMetricSpecification:
        PredefinedMetricType: ECSServiceAverageCPUUtilization
      TargetValue: 70.0
      ScaleInCooldown: 300
      ScaleOutCooldown: 60
```

### Lambda

**Cold Start Optimization:**

| Technique | Impact | When |
|-----------|--------|------|
| Smaller package | High | Always — strip devDeps, tree-shake |
| ARM64 (Graviton) | 20% cheaper + faster | Always unless native deps need x86 |
| Provisioned concurrency | Eliminates cold starts | Latency-sensitive APIs |
| SnapStart (Java) | 10x faster cold start | Java only |
| Keep-warm ping | Reduces frequency | Low-traffic functions |
| Lazy init | Faster cold start | Move SDK init outside handler |

**Lambda Layers:**
```bash
# Create layer
mkdir -p layer/nodejs
cd layer/nodejs && npm install sharp --platform=linux --arch=arm64
cd .. && zip -r sharp-layer.zip nodejs/
aws lambda publish-layer-version \
  --layer-name sharp-arm64 \
  --zip-file fileb://sharp-layer.zip \
  --compatible-runtimes nodejs20.x \
  --compatible-architectures arm64
```

**Lambda Destinations (async):**
```yaml
EventInvokeConfig:
  Type: AWS::Lambda::EventInvokeConfig
  Properties:
    FunctionName: !Ref MyFunction
    MaximumRetryAttempts: 2
    DestinationConfig:
      OnSuccess:
        Destination: !GetAtt SuccessQueue.Arn
      OnFailure:
        Destination: !GetAtt DLQ.Arn
```

**Our Lambda Services:**
- `excise-wine-authen-staging`: Node.js 20, Cognito triggers, apse7
- `excise-car-cron`: Node.js 20, EventBridge scheduled, apse7

**Lambda Gotchas (from our pipelines):**
- `npm ci` in `dist/` requires BOTH `package.json` AND `package-lock.json`
- Always guard zip creation: `if [ ! -f "function.zip" ]; then exit 1; fi`
- Max 250MB unzipped (layers included). Use S3 for larger payloads
- `/tmp` is 512MB default, configurable up to 10GB
- Execution timeout max 15 minutes. Use Step Functions for longer

### EKS

**When EKS vs ECS:**

| Factor | EKS | ECS |
|--------|-----|-----|
| Team K8s expertise | Required | Not needed |
| Multi-cloud portability | Yes (standard K8s) | AWS-only |
| Ecosystem (Helm, operators) | Massive | Limited |
| Cost (control plane) | $0.10/hr (~$73/mo) | Free |
| Complexity | High | Low |
| Best for | Complex microservices, multi-cloud | AWS-native, simpler setups |

**EKS Best Practices:**
- Managed node groups with Graviton for cost
- Karpenter for node autoscaling (replaces Cluster Autoscaler)
- IRSA (IAM Roles for Service Accounts) for pod-level IAM
- CoreDNS + external-dns for DNS management
- AWS Load Balancer Controller for ALB/NLB integration
- EBS CSI driver for persistent volumes
- Secrets Store CSI Driver for Secrets Manager integration

## 1.2 Networking

### VPC Architecture

**Standard 3-tier VPC:**
```
VPC: 10.0.0.0/16 (65,536 IPs)
├── Public Subnets (ALB, NAT Gateway, Bastion)
│   ├── 10.0.1.0/24 (AZ-a, 254 IPs)
│   ├── 10.0.2.0/24 (AZ-b, 254 IPs)
│   └── 10.0.3.0/24 (AZ-c, 254 IPs)
├── Private Subnets (ECS, EC2, Lambda)
│   ├── 10.0.11.0/24 (AZ-a)
│   ├── 10.0.12.0/24 (AZ-b)
│   └── 10.0.13.0/24 (AZ-c)
└── Data Subnets (RDS, ElastiCache)
    ├── 10.0.21.0/24 (AZ-a)
    ├── 10.0.22.0/24 (AZ-b)
    └── 10.0.23.0/24 (AZ-c)
```

**Our VPC:** `vpc-0b10104e1ed5ae1f7` in apse7

**VPC Endpoints (save NAT Gateway costs):**

| Type | Service | Cost |
|------|---------|------|
| Gateway (free) | S3, DynamoDB | $0 |
| Interface ($) | ECR, Secrets Manager, CloudWatch, STS, SSM | ~$7.30/mo/endpoint/AZ |

```bash
# Gateway endpoint for S3 (free, always use)
aws ec2 create-vpc-endpoint \
  --vpc-id vpc-0b10104e1ed5ae1f7 \
  --service-name com.amazonaws.ap-southeast-7.s3 \
  --route-table-ids rtb-xxxxx
```

**NAT Gateway:**
- $0.045/hr + $0.045/GB processed in apse1
- Use single NAT GW for non-prod (cost saving)
- Use per-AZ NAT GW for prod (HA)
- Alternative: NAT instances on t4g.nano for dev (~$3/mo vs $32/mo)

**VPC Peering vs Transit Gateway:**

| Factor | VPC Peering | Transit Gateway |
|--------|-------------|-----------------|
| Scale | 1:1, max 125 peers | Hub-spoke, 5000 attachments |
| Transitive | No | Yes |
| Cost | Free (data transfer only) | $0.05/hr + $0.02/GB |
| Cross-region | Yes | Yes |
| Best for | 2-3 VPCs | Many VPCs, complex routing |

### ALB / NLB

**ALB (Layer 7):**
```yaml
# Weighted target groups (canary deployment)
ListenerRule:
  Type: AWS::ElasticLoadBalancingV2::ListenerRule
  Properties:
    Actions:
      - Type: forward
        ForwardConfig:
          TargetGroups:
            - TargetGroupArn: !Ref StableTG
              Weight: 90
            - TargetGroupArn: !Ref CanaryTG
              Weight: 10
    Conditions:
      - Field: path-pattern
        Values: ["/api/*"]
```

**NLB (Layer 4):**
- TCP/UDP, millions of requests/sec
- Static IP per AZ (or Elastic IP)
- Preserves source IP
- Use for: gRPC, WebSocket, non-HTTP, extreme throughput

**Sticky Sessions:**
- ALB: Application-based cookies (`AWSALB`) or duration-based
- Anti-pattern: relying on sticky sessions instead of stateless design
- If you need sessions: use Redis/DynamoDB for session store

### Route53

**Routing Policies:**

| Policy | Use Case |
|--------|----------|
| Simple | Single resource |
| Weighted | A/B testing, canary (90/10) |
| Latency | Multi-region, route to lowest latency |
| Failover | Active-passive HA |
| Geolocation | Country/continent-based routing |
| Geoproximity | Bias traffic toward specific regions |
| Multivalue | Simple load balancing with health checks |

**Health Checks:**
```bash
aws route53 create-health-check --caller-reference $(date +%s) \
  --health-check-config '{
    "Type": "HTTPS",
    "FullyQualifiedDomainName": "api.example.com",
    "Port": 443,
    "ResourcePath": "/health",
    "RequestInterval": 30,
    "FailureThreshold": 3,
    "EnableSNI": true
  }'
```

**Failover Pattern:**
```
Primary (apse7) --health-check--> Route53 --failover--> Secondary (apse1)
                    UNHEALTHY -----> switch DNS to secondary
```

### CloudFront

**Behaviors Configuration:**
```yaml
Distribution:
  Type: AWS::CloudFront::Distribution
  Properties:
    DistributionConfig:
      DefaultCacheBehavior:
        TargetOriginId: ALBOrigin
        ViewerProtocolPolicy: redirect-to-https
        CachePolicyId: 658327ea-f89d-4fab-a63d-7e88639e58f6  # CachingOptimized
        OriginRequestPolicyId: 216adef6-5c7f-47e4-b989-5492eafa07d3  # AllViewer
      CacheBehaviors:
        - PathPattern: "/api/*"
          TargetOriginId: ALBOrigin
          CachePolicyId: 4135ea2d-6df8-44a3-9df3-4b5a84be39ad  # CachingDisabled
          OriginRequestPolicyId: 216adef6-5c7f-47e4-b989-5492eafa07d3
        - PathPattern: "/static/*"
          TargetOriginId: S3Origin
          CachePolicyId: 658327ea-f89d-4fab-a63d-7e88639e58f6  # CachingOptimized
          TTL: 86400
```

**Lambda@Edge vs CloudFront Functions:**

| Feature | Lambda@Edge | CloudFront Functions |
|---------|-------------|---------------------|
| Runtime | Node.js, Python | JavaScript only |
| Execution time | 5s (viewer), 30s (origin) | <1ms |
| Memory | 128MB-10GB | 2MB |
| Network access | Yes | No |
| Cost | $0.60/million | $0.10/million |
| Use case | Auth, A/B test, rewrite | Header manipulation, redirects, URL rewrite |

**Cache Invalidation:**
```bash
aws cloudfront create-invalidation \
  --distribution-id E1234567890 \
  --paths "/*"
# Cost: first 1000 free/mo, then $0.005/path
# Prefer versioned URLs (style.v3.css) over invalidation
```

### WAF

**Standard Rule Groups:**
```yaml
WebACL:
  Type: AWS::WAFv2::WebACL
  Properties:
    Scope: REGIONAL  # or CLOUDFRONT
    DefaultAction: {Allow: {}}
    Rules:
      - Name: AWSManagedRulesCommonRuleSet
        Priority: 1
        Statement:
          ManagedRuleGroupStatement:
            VendorName: AWS
            Name: AWSManagedRulesCommonRuleSet
        OverrideAction: {None: {}}
        VisibilityConfig: {SampledRequestsEnabled: true, CloudWatchMetricsEnabled: true, MetricName: CommonRules}
      - Name: AWSManagedRulesSQLiRuleSet
        Priority: 2
        Statement:
          ManagedRuleGroupStatement:
            VendorName: AWS
            Name: AWSManagedRulesSQLiRuleSet
        OverrideAction: {None: {}}
        VisibilityConfig: {SampledRequestsEnabled: true, CloudWatchMetricsEnabled: true, MetricName: SQLiRules}
      - Name: RateLimit
        Priority: 3
        Statement:
          RateBasedStatement:
            Limit: 2000  # per 5 minutes per IP
            AggregateKeyType: IP
        Action: {Block: {}}
        VisibilityConfig: {SampledRequestsEnabled: true, CloudWatchMetricsEnabled: true, MetricName: RateLimit}
```

**IP Set for Allowlist/Blocklist:**
```bash
aws wafv2 create-ip-set \
  --name BlockedIPs \
  --scope REGIONAL \
  --ip-address-version IPV4 \
  --addresses "1.2.3.4/32" "5.6.7.0/24"
```

## 1.3 Storage

### S3

**Lifecycle Policies:**
```json
{
  "Rules": [{
    "ID": "ArchiveOldArtifacts",
    "Status": "Enabled",
    "Filter": {"Prefix": "artifacts/"},
    "Transitions": [
      {"Days": 30, "StorageClass": "STANDARD_IA"},
      {"Days": 90, "StorageClass": "GLACIER_IR"},
      {"Days": 365, "StorageClass": "DEEP_ARCHIVE"}
    ],
    "NoncurrentVersionTransitions": [
      {"NoncurrentDays": 30, "StorageClass": "GLACIER_IR"}
    ],
    "NoncurrentVersionExpiration": {"NoncurrentDays": 90}
  }]
}
```

**Storage Classes Decision:**

| Class | Access | Min Duration | Use Case |
|-------|--------|-------------|----------|
| Standard | Frequent | None | Active data |
| Intelligent-Tiering | Unknown | None | Unpredictable access |
| Standard-IA | Infrequent | 30 days | Backups, DR |
| One Zone-IA | Infrequent | 30 days | Reproducible data |
| Glacier IR | Rare (ms retrieval) | 90 days | Compliance archives |
| Glacier Flexible | Rare (min-hrs) | 90 days | Long-term archives |
| Deep Archive | Very rare (12hrs) | 180 days | Regulatory retention |

**Presigned URLs:**
```bash
# Upload (PUT)
aws s3 presign s3://bucket/key --expires-in 3600

# Programmatic (boto3)
url = s3.generate_presigned_url('put_object',
    Params={'Bucket': 'bucket', 'Key': 'upload/file.pdf', 'ContentType': 'application/pdf'},
    ExpiresIn=3600)
```

**Multipart Upload (>100MB):**
```bash
aws s3 cp large-file.tar.gz s3://bucket/key \
  --expected-size 5368709120 \
  --storage-class STANDARD_IA
# SDK: initiate -> upload parts -> complete (or abort)
# Auto-multipart threshold: 8MB default in CLI
```

**Cross-Region Replication:**
```yaml
ReplicationConfiguration:
  Role: !GetAtt ReplicationRole.Arn
  Rules:
    - Status: Enabled
      Destination:
        Bucket: !Sub "arn:aws:s3:::${DestBucket}"
        StorageClass: STANDARD_IA
      Filter:
        Prefix: "critical/"
```

**S3 Event Notifications:**
```yaml
NotificationConfiguration:
  EventBridgeConfiguration:
    EventBridgeEnabled: true  # Preferred — route all events through EventBridge
  LambdaConfigurations:
    - Event: "s3:ObjectCreated:*"
      Filter: {S3Key: {Rules: [{Name: prefix, Value: "uploads/"}]}}
      Function: !GetAtt ProcessorFunction.Arn
```

**Our S3 Structure:**
```
thinkbit-devops-artifacts/
├── devops/devops-tools/
│   ├── buildspec-template/    # Universal buildspec templates
│   ├── ci-template/           # CI CF templates
│   ├── cd-template/           # CD CF templates
│   └── devops-tool-thirdparty/
├── lambda/{stack-name}/{version}/  # Lambda zips + checksums
├── thinkbit/{stack-name}/{version}/ # ECS/EC2 templates + params
└── devops/template/{stack-name}/    # Legacy path (migration planned)
```

### EBS

**Volume Types:**

| Type | IOPS | Throughput | Use Case |
|------|------|-----------|----------|
| gp3 | 3,000 baseline (up to 16,000) | 125 MB/s (up to 1,000) | Default, most workloads |
| io2 Block Express | Up to 256,000 | 4,000 MB/s | Mission-critical DBs |
| st1 | 500 baseline | 500 MB/s | Sequential reads (logs, data warehouse) |
| sc1 | 250 baseline | 250 MB/s | Infrequent access, cold storage |

Always use gp3 over gp2 — same price, better baseline performance, independent IOPS/throughput tuning.

### EFS

```bash
# Mount target in each AZ subnet
aws efs create-mount-target \
  --file-system-id fs-12345 \
  --subnet-id subnet-xxxxx \
  --security-groups sg-xxxxx

# Mount (NFS4.1)
mount -t efs -o tls fs-12345:/ /mnt/efs
# Or in /etc/fstab:
# fs-12345:/ /mnt/efs efs _netdev,tls 0 0
```

EFS One Zone for 47% cost savings when HA across AZs not needed.

## 1.4 Database

### RDS

**Multi-AZ Deployments:**

| Feature | Multi-AZ Instance | Multi-AZ Cluster |
|---------|-------------------|-------------------|
| Replicas | 1 standby (sync) | 2 readers (sync) |
| Failover | 60-120s | <35s |
| Read traffic | No (standby only) | Yes (reader endpoints) |
| Engines | All | MySQL, PostgreSQL |

**Our RDS:**
- Instance: `think-bit-rds`
- Engine: SQL Server SE (`sqlserver-se`)
- Class: `db.m6i.2xlarge`
- Storage: 300GB gp3
- Region: ap-southeast-7

**RDS Proxy:**
```hcl
# From our Terraform (thinkbit-devops-iac)
resource "aws_db_proxy" "rds_proxy" {
  name                   = "think-bit-rds-proxy"
  engine_family          = "SQLSVR"
  role_arn               = aws_iam_role.rds_proxy.arn
  vpc_subnet_ids         = var.subnet_ids
  vpc_security_group_ids = [aws_security_group.rds_proxy.id]

  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_secretsmanager_secret.db_creds.arn
    iam_auth    = "DISABLED"
  }
}
```

**Parameter Groups (critical settings):**
```
# PostgreSQL
shared_buffers = {DBInstanceClassMemory/4}
effective_cache_size = {DBInstanceClassMemory*3/4}
max_connections = LEAST({DBInstanceClassMemory/9531392}, 5000)
work_mem = 4MB-64MB (depends on concurrent queries)
maintenance_work_mem = 512MB-2GB
random_page_cost = 1.1 (for SSD)
checkpoint_completion_target = 0.9
wal_buffers = 64MB

# MySQL/Aurora
innodb_buffer_pool_size = {DBInstanceClassMemory*3/4}
innodb_log_file_size = 2GB
max_connections = {DBInstanceClassMemory/12582880}
innodb_flush_log_at_trx_commit = 1 (ACID) or 2 (performance)
```

**Maintenance Windows:**
- Schedule during lowest traffic (e.g., Sun 03:00-05:00 UTC+7)
- Enable auto minor version upgrade for security patches
- Test major upgrades in staging with snapshot restore first
- Blue/green deployments for zero-downtime major upgrades (RDS Blue/Green)

### Aurora

**Aurora Serverless v2:**
```yaml
RDSCluster:
  Type: AWS::RDS::DBCluster
  Properties:
    Engine: aurora-postgresql
    EngineVersion: "15.4"
    ServerlessV2ScalingConfiguration:
      MinCapacity: 0.5    # 1 GB RAM
      MaxCapacity: 64      # 128 GB RAM
    # Scales in 15-second increments
```

**Aurora Global Database:**
- Primary in apse7, secondary in apse1
- <1s replication lag
- RPO: typically <1s, RTO: <1 minute (managed failover)
- Use for DR and read scaling across regions

### DynamoDB

**Single-Table Design Patterns:**
```
PK              | SK                  | Data
USER#123        | PROFILE             | {name, email, ...}
USER#123        | ORDER#2024-001      | {total, status, ...}
USER#123        | ORDER#2024-002      | {total, status, ...}
ORDER#2024-001  | ITEM#abc            | {product, qty, price}
ORDER#2024-001  | ITEM#def            | {product, qty, price}
```

**Access Pattern -> Index Design:**
```
Access Pattern                  | Key Condition
Get user profile                | PK=USER#123, SK=PROFILE
List user orders                | PK=USER#123, SK begins_with ORDER#
Get order items                 | PK=ORDER#2024-001, SK begins_with ITEM#
Orders by status (GSI1)         | GSI1PK=STATUS#shipped, GSI1SK=ORDER#timestamp
```

**Capacity Modes:**
- On-Demand: unpredictable, spiky, new tables. No capacity planning needed
- Provisioned: steady-state, predictable. Use auto-scaling with target 70% utilization
- Reserved: 1-year or 3-year commitment for large tables (up to 77% savings)

**DynamoDB Streams + Lambda:**
```yaml
StreamSpecification:
  StreamViewType: NEW_AND_OLD_IMAGES  # or KEYS_ONLY, NEW_IMAGE, OLD_IMAGE
# Lambda processes stream records (change data capture)
# Use for: replication, materialized views, event sourcing, audit logs
```

**TTL:**
```bash
# Enable TTL on attribute 'expiresAt' (epoch seconds)
aws dynamodb update-time-to-live \
  --table-name Sessions \
  --time-to-live-specification "Enabled=true,AttributeName=expiresAt"
# Items deleted within 48 hours of TTL expiry (eventually consistent)
```

### ElastiCache (Redis)

**Cluster Mode:**

| Feature | Cluster Mode Disabled | Cluster Mode Enabled |
|---------|----------------------|---------------------|
| Sharding | No (single shard) | Yes (up to 500 shards) |
| Max data | ~340GB (r7g.16xlarge) | 170TB+ |
| Write scaling | Vertical only | Horizontal |
| Multi-AZ | Yes (1 primary + replicas) | Yes (per shard) |

**Eviction Policies:**
- `volatile-lru`: Evict LRU keys with TTL set (default, good for caching)
- `allkeys-lru`: Evict LRU from all keys (when all data is cache)
- `volatile-ttl`: Evict shortest TTL first
- `noeviction`: Return error on write when full (for persistent data)

**Memory Optimization:**
```
maxmemory-policy volatile-lru
maxmemory 75%  # Leave headroom for fragmentation
lazyfree-lazy-eviction yes
lazyfree-lazy-expire yes
```

## 1.5 Security

### IAM

**Policy Structure:**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowECRPull",
    "Effect": "Allow",
    "Action": [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability"
    ],
    "Resource": "arn:aws:ecr:ap-southeast-7:498952158610:repository/excise-*",
    "Condition": {
      "StringEquals": {
        "aws:RequestedRegion": ["ap-southeast-1", "ap-southeast-7"]
      }
    }
  }]
}
```

**Condition Keys (most useful):**

| Condition | Use |
|-----------|-----|
| `aws:RequestedRegion` | Restrict to specific regions |
| `aws:PrincipalOrgID` | Allow from organization only |
| `aws:SourceVpc` | VPC-only access |
| `aws:SourceIp` | IP allowlist (use with caution) |
| `aws:MultiFactorAuthPresent` | Require MFA |
| `aws:PrincipalTag/Department` | ABAC (attribute-based) |
| `s3:prefix` | Restrict S3 access to key prefix |
| `ec2:ResourceTag/Environment` | Tag-based resource access |

**Permission Boundaries:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "*",
      "Resource": "*"
    },
    {
      "Effect": "Deny",
      "Action": ["iam:CreateUser", "iam:CreateRole", "organizations:*", "account:*"],
      "Resource": "*"
    }
  ]
}
```

**SCPs (Service Control Policies):**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyNonApprovedRegions",
      "Effect": "Deny",
      "Action": "*",
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:RequestedRegion": ["ap-southeast-1", "ap-southeast-7", "us-east-1"]
        },
        "ArnNotLike": {
          "aws:PrincipalARN": "arn:aws:iam::*:role/OrganizationAdmin"
        }
      }
    }
  ]
}
```

**Our IAM Security Issues (from AWS-Fix-Assessment):**
- 7 IAM users with AdministratorAccess — need scoping
- Root: 2 active access keys (DELETE immediately)
- Root MFA: virtual -> needs hardware FIDO2 key
- 85 ECS task defs with env var secrets (migrating to Secrets Manager)
- 9 Lambda functions with hardcoded secrets

### Secrets Manager vs SSM Parameter Store

| Feature | Secrets Manager | SSM Parameter Store |
|---------|----------------|-------------------|
| Cost | $0.40/secret/mo + $0.05/10K calls | Free (standard), $0.05/10K (advanced) |
| Rotation | Built-in Lambda rotation | Manual |
| Cross-account | Native | Via resource policy |
| Max size | 64KB | 8KB (standard), 8KB (advanced) |
| Best for | DB creds, API keys, rotation needed | Config values, feature flags, non-rotating |

**Our Pattern:**
- Secrets Manager: DB passwords, API keys, Cognito secrets
- SSM Parameter Store: build numbers (`VERSION_BUILDNUMBER_PARAM`), config values, stack parameters

### KMS

```bash
# Create CMK
aws kms create-key --description "Excise data encryption" \
  --key-usage ENCRYPT_DECRYPT --key-spec SYMMETRIC_DEFAULT

# Key policy: allow account + specific roles
# Alias for human-readable reference
aws kms create-alias --alias-name alias/excise-data --target-key-id <key-id>
```

**Envelope Encryption Pattern:**
1. Generate data key with KMS (`GenerateDataKey`)
2. Encrypt data with plaintext data key (local, fast)
3. Store encrypted data + encrypted data key together
4. Discard plaintext data key from memory

### Security Hub / GuardDuty / Config

**Security Hub:**
- Aggregates findings from GuardDuty, Inspector, Config, Macie, IAM Access Analyzer
- CIS AWS Foundations Benchmark (automated compliance checks)
- AWS Foundational Security Best Practices standard
- Enable in all regions, designate admin account

**GuardDuty:**
- ML-based threat detection (no agents needed)
- Monitors: CloudTrail, VPC Flow Logs, DNS, S3, EKS, Lambda, RDS
- Finding types: Recon, UnauthorizedAccess, Crypto mining, data exfil
- Enable in all regions, 30-day free trial

**Config Rules:**
```bash
# Example: ensure S3 buckets are encrypted
aws configservice put-config-rule --config-rule '{
  "ConfigRuleName": "s3-bucket-server-side-encryption-enabled",
  "Source": {"Owner": "AWS", "SourceIdentifier": "S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED"},
  "Scope": {"ComplianceResourceTypes": ["AWS::S3::Bucket"]}
}'
```

## 1.6 CI/CD (AWS)

### CodeBuild

**Our Standard CodeBuild Setup:**
- Image: `amazonlinux2-x86_64-standard:5.0`
- Compute: `BUILD_GENERAL1_SMALL`
- Region: ap-southeast-1 (always)
- Secondary source: `thinkbit-devops-modules` (DEVOPS_MODULES)

**Buildspec Structure:**
```yaml
version: 0.2
env:
  variables:
    DEPLOY_REGION: ap-southeast-7
  parameter-store:
    BUILD_NUMBER: /excise-wine-nodejs-api-staging/build-number
  secrets-manager:
    GITHUB_TOKEN: github-deploy-pat
phases:
  install:
    runtime-versions:
      nodejs: 20
    commands:
      - npm ci --omit=dev
  pre_build:
    commands:
      # DevSecOps pipeline
      - pip3 install gitleaks semgrep cfn-lint || true
      - gitleaks detect --source . --no-banner || echo "WARN: gitleaks findings"
      - |
        if command -v semgrep && semgrep --version 2>/dev/null; then
          semgrep --config auto --error --quiet . || echo "WARN: semgrep findings"
        else
          echo "WARN: semgrep not available on this platform"
        fi
      - npm audit --omit=dev --audit-level=critical
  build:
    commands:
      - npm run build
      - docker build -t $ECR_REPO:$BUILD_NUMBER .
      - docker push $ECR_REPO:$BUILD_NUMBER
  post_build:
    commands:
      # Cross-region sync (FATAL)
      - aws s3 sync s3://thinkbit-devops-artifacts/thinkbit/$STACK_NAME/ \
          s3://thinkbit-devops-artifacts-apse7/thinkbit/$STACK_NAME/ \
          --source-region ap-southeast-1 --region ap-southeast-7
      - |
        if [ $? -ne 0 ]; then
          echo "FATAL: Cross-region sync failed"
          exit 1
        fi
      # Deploy trigger
      - aws s3 cp deployment-package.zip \
          s3://thinkbit-devops-artifacts/devops/template/$STACK_NAME/
artifacts:
  files:
    - build-info.json
cache:
  paths:
    - 'node_modules/**/*'
    - '/root/.npm/**/*'
```

**CodeBuild VPC Access (for private resources):**
```yaml
VpcConfig:
  VpcId: vpc-0b10104e1ed5ae1f7
  Subnets: [subnet-private-a, subnet-private-b]
  SecurityGroupIds: [sg-codebuild]
# Needs NAT Gateway for internet access when in VPC
```

**Known Issues:**
- semgrep on Amazon Linux 2: `semgrep-core` binary often missing. Always check `semgrep --version` first
- npm audit: use `--omit=dev` to reduce false positives, `--audit-level=critical` to block only critical
- Cross-region IAM: CodeBuild roles need explicit S3 permission for apse7 bucket

### CodePipeline + CodeDeploy

**Blue/Green with CodeDeploy:**
```yaml
# appspec.yml (ECS)
version: 0.0
Resources:
  - TargetService:
      Type: AWS::ECS::Service
      Properties:
        TaskDefinition: <TASK_DEFINITION>
        LoadBalancerInfo:
          ContainerName: "app"
          ContainerPort: 3000

# In-place (EC2)
version: 0.0
hooks:
  BeforeInstall:
    - location: scripts/stop.sh
      timeout: 300
  AfterInstall:
    - location: scripts/install.sh
      timeout: 300
  ApplicationStart:
    - location: scripts/start.sh
      timeout: 300
  ValidateService:
    - location: scripts/validate.sh
      timeout: 300
```

**Our Deploy Pattern:**
- `deployment-package.zip` uploaded to S3 -> EventBridge -> CodeBuild deploy -> CF update in apse7
- Deploy buildspec reads `{stack-name}-template.yaml` + `{stack-name}-params.json` from zip
- Runs `aws cloudformation create-stack` or `update-stack` in apse7

### EventBridge

```yaml
Rule:
  Type: AWS::Events::Rule
  Properties:
    EventPattern:
      source: ["aws.s3"]
      detail-type: ["Object Created"]
      detail:
        bucket: {name: ["thinkbit-devops-artifacts"]}
        object: {key: [{prefix: "devops/template/excise-wine-"}]}
    Targets:
      - Id: TriggerDeploy
        Arn: !GetAtt DeployProject.Arn
        RoleArn: !GetAtt EventBridgeRole.Arn
```

## 1.7 Serverless

### API Gateway

**REST vs HTTP API:**

| Feature | REST API | HTTP API |
|---------|----------|----------|
| Cost | $3.50/million | $1.00/million |
| Latency | Higher | 60% lower |
| Features | Full (caching, API keys, usage plans, WAF) | Basic (JWT auth, CORS) |
| Auth | IAM, Cognito, Lambda, API Key | IAM, JWT, Lambda |
| Best for | Public APIs with quotas | Internal/simple APIs |

### Step Functions

```json
{
  "Comment": "Order processing workflow",
  "StartAt": "ValidateOrder",
  "States": {
    "ValidateOrder": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:ap-southeast-7:498952158610:function:validate-order",
      "Retry": [{"ErrorEquals": ["States.TaskFailed"], "IntervalSeconds": 3, "MaxAttempts": 2, "BackoffRate": 2}],
      "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "FailureHandler"}],
      "Next": "ProcessPayment"
    },
    "ProcessPayment": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
      "Parameters": {
        "QueueUrl": "https://sqs.ap-southeast-7.amazonaws.com/498952158610/payments",
        "MessageBody": {"taskToken.$": "$$.Task.Token", "orderId.$": "$.orderId"}
      },
      "TimeoutSeconds": 3600,
      "Next": "FulfillOrder"
    },
    "FulfillOrder": {
      "Type": "Parallel",
      "Branches": [
        {"StartAt": "SendConfirmation", "States": {"SendConfirmation": {"Type": "Task", "Resource": "arn:aws:lambda:...", "End": true}}},
        {"StartAt": "UpdateInventory", "States": {"UpdateInventory": {"Type": "Task", "Resource": "arn:aws:lambda:...", "End": true}}}
      ],
      "End": true
    },
    "FailureHandler": {"Type": "Task", "Resource": "arn:aws:lambda:...", "End": true}
  }
}
```

**Express vs Standard:**
- Standard: long-running (up to 1 year), exactly-once, $0.025/1000 transitions
- Express: short (5 min max), at-least-once, $1/million + duration. For high-volume event processing

### SQS / SNS

**SQS Best Practices:**
```
Standard Queue:
  - At-least-once delivery, best-effort ordering
  - Throughput: unlimited (batching recommended)
  - Use for: decoupling, buffering, async processing

FIFO Queue:
  - Exactly-once, strict ordering per MessageGroupId
  - Throughput: 300 msg/s (3000 with batching, 70K with high throughput mode)
  - Use for: financial transactions, order processing

DLQ Pattern:
  Queue -> Lambda (maxRetries=3) -> DLQ (after 3 failures)
  DLQ -> separate Lambda (investigation/reprocessing)
  Set maxReceiveCount=3 on redrive policy
```

**SNS Fan-Out:**
```
SNS Topic -> SQS Queue 1 (service A)
          -> SQS Queue 2 (service B)
          -> Lambda (analytics)
          -> HTTP endpoint (webhook)
```

## 1.8 Monitoring (AWS)

### CloudWatch

**Logs Insights Queries:**
```
# Error rate over time
filter @message like /ERROR/
| stats count(*) as errors by bin(5m)

# p99 latency
filter @message like /duration/
| stats pct(@duration, 99) as p99, avg(@duration) as avg_duration by bin(5m)

# Top 10 slowest requests
filter @duration > 0
| sort @duration desc
| limit 10

# Lambda cold starts
filter @type = "REPORT"
| stats count(*) as invocations,
        sum(@initDuration > 0) as cold_starts,
        avg(@initDuration) as avg_cold_start_ms
  by bin(1h)
```

**Custom Metrics:**
```bash
aws cloudwatch put-metric-data \
  --namespace "Excise/Wine" \
  --metric-name "OrdersProcessed" \
  --value 42 \
  --unit Count \
  --dimensions "Service=nodejs-api,Environment=staging"
```

**Composite Alarms:**
```yaml
CompositeAlarm:
  Type: AWS::CloudWatch::CompositeAlarm
  Properties:
    AlarmName: HighSeverityAlert
    AlarmRule: |
      ALARM("HighErrorRate") AND
      (ALARM("HighLatency") OR ALARM("HighCPU"))
    AlarmActions: [!Ref SNSAlertTopic]
```

**Contributor Insights:**
```bash
# Find top contributors to errors
aws cloudwatch put-insight-rule --rule-name TopErrorPaths \
  --rule-body '{
    "Schema": {"Name": "CloudWatchLogRule", "Version": 1},
    "LogGroupNames": ["/ecs/excise-wine-nodejs-api-staging"],
    "LogFormat": "JSON",
    "Contribution": {
      "Keys": ["$.path"],
      "Filters": [{"Match": "$.statusCode", "GreaterThan": 499}],
      "ValueKey": "$.statusCode"
    },
    "AggregateOn": "Count"
  }' --rule-state ENABLED
```

### X-Ray

```bash
# Enable X-Ray daemon in ECS task
{
  "name": "xray-daemon",
  "image": "amazon/aws-xray-daemon",
  "portMappings": [{"containerPort": 2000, "protocol": "udp"}],
  "cpu": 32, "memoryReservation": 256
}

# Sampling rules (reduce cost)
{
  "version": 2,
  "default": {"fixed_target": 1, "rate": 0.05},
  "rules": [
    {"description": "Health checks", "host": "*", "http_method": "GET",
     "url_path": "/health", "fixed_target": 0, "rate": 0},
    {"description": "API", "host": "*", "http_method": "*",
     "url_path": "/api/*", "fixed_target": 1, "rate": 0.1}
  ]
}
```

## 1.9 Cost Optimization

### Savings Plans vs Reserved Instances

| Feature | Savings Plans | Reserved Instances |
|---------|--------------|-------------------|
| Flexibility | High (any instance family) | Low (specific instance type) |
| Discount | Up to 72% (Compute SP) | Up to 72% |
| Commitment | $/hr | Instance type |
| Applies to | EC2, Fargate, Lambda | EC2 only |
| Recommendation | Prefer Compute Savings Plans | Only for steady RDS/ElastiCache |

### Spot Instances

```bash
# Spot Fleet (diversified, handles interruptions)
aws ec2 request-spot-fleet --spot-fleet-request-config '{
  "IamFleetRole": "arn:aws:iam::498952158610:role/spot-fleet",
  "TargetCapacity": 10,
  "LaunchSpecifications": [
    {"InstanceType": "c7g.large", "SubnetId": "subnet-a", "WeightedCapacity": 1},
    {"InstanceType": "c6g.large", "SubnetId": "subnet-a", "WeightedCapacity": 1},
    {"InstanceType": "c7g.xlarge", "SubnetId": "subnet-b", "WeightedCapacity": 2}
  ],
  "AllocationStrategy": "capacityOptimized"
}'
# Handle interruption: 2-minute warning via instance metadata or EventBridge
```

**Spot Best Practices:**
- Diversify across 4+ instance types and 3+ AZs
- Use `capacityOptimized` allocation strategy
- Handle 2-minute interruption warning gracefully (drain connections, save state)
- Use for: batch processing, CI/CD workers, fault-tolerant workloads
- Never for: databases, stateful services, single-instance workloads

### Right-Sizing

```bash
# CloudWatch agent for memory metrics (EC2 default lacks memory)
# Then use Cost Explorer right-sizing recommendations
aws ce get-rightsizing-recommendation \
  --service AmazonEC2 \
  --configuration '{
    "RecommendationTarget": "SAME_INSTANCE_FAMILY",
    "BenefitsConsidered": true
  }'
```

### Cost Anomaly Detection

```bash
aws ce create-anomaly-monitor --anomaly-monitor '{
  "MonitorName": "ExciseServiceCosts",
  "MonitorType": "DIMENSIONAL",
  "MonitorDimension": "SERVICE"
}'
aws ce create-anomaly-subscription --anomaly-subscription '{
  "MonitorArnList": ["arn:aws:ce::498952158610:anomalymonitor/..."],
  "Frequency": "DAILY",
  "Threshold": 20,
  "Subscribers": [{"Address": "alerts@devthinkbit.com", "Type": "EMAIL"}]
}'
```

---

# 2. GCP

## 2.1 Compute

### Compute Engine
```bash
# Create VM with Container-Optimized OS
gcloud compute instances create web-server \
  --machine-type=e2-medium \
  --zone=asia-southeast1-b \
  --image-family=cos-stable --image-project=cos-cloud \
  --tags=http-server,https-server \
  --metadata=startup-script='#!/bin/bash
    docker run -d -p 80:8080 gcr.io/myproject/app:latest'

# Instance types
# E2: cost-optimized, burstable (like t3)
# N2/N2D: general purpose (like m5/m5a)
# C3/C3D: compute optimized (like c5)
# M3: memory optimized (like r5)
# T2A: Arm (Ampere Altra) — GCP's Graviton equivalent
# A3: GPU (H100)
```

### Cloud Run
```bash
# Deploy container (fully managed, scale to zero)
gcloud run deploy myservice \
  --image=asia-southeast1-docker.pkg.dev/myproject/repo/app:v1 \
  --region=asia-southeast1 \
  --platform=managed \
  --memory=512Mi --cpu=1 \
  --min-instances=0 --max-instances=100 \
  --set-env-vars="NODE_ENV=production" \
  --set-secrets="DB_PASS=db-password:latest" \
  --allow-unauthenticated  # or --no-allow-unauthenticated for internal

# Cloud Run vs App Engine vs Cloud Functions:
# Cloud Run: containers, any language, scale-to-zero, request-based (preferred)
# Cloud Functions: single-purpose functions, event-driven
# App Engine: legacy PaaS, prefer Cloud Run for new projects
```

### GKE
```bash
# Create Autopilot cluster (recommended — no node management)
gcloud container clusters create-auto myapp \
  --region=asia-southeast1 \
  --release-channel=regular

# Standard cluster (when you need node control)
gcloud container clusters create myapp \
  --region=asia-southeast1 \
  --num-nodes=3 \
  --machine-type=e2-standard-4 \
  --enable-autoscaling --min-nodes=1 --max-nodes=10 \
  --enable-network-policy \
  --workload-pool=myproject.svc.id.goog  # Workload Identity
```

## 2.2 Database

### Cloud SQL
```bash
gcloud sql instances create mydb \
  --database-version=POSTGRES_15 \
  --tier=db-custom-4-16384 \
  --region=asia-southeast1 \
  --availability-type=REGIONAL \
  --storage-type=SSD --storage-size=100GB \
  --storage-auto-increase \
  --backup-start-time=02:00 \
  --maintenance-window-day=SUN --maintenance-window-hour=3 \
  --database-flags=max_connections=500,shared_buffers=4096MB

# Connection: Cloud SQL Auth Proxy (recommended)
cloud-sql-proxy --port=5432 myproject:asia-southeast1:mydb
```

### Firestore
```python
# Native mode (document DB, real-time)
from google.cloud import firestore
db = firestore.Client()

# Write
db.collection('users').document('uid123').set({
    'name': 'Test', 'created': firestore.SERVER_TIMESTAMP
})

# Query with composite index
users = db.collection('users') \
    .where('status', '==', 'active') \
    .where('age', '>=', 18) \
    .order_by('age') \
    .limit(50) \
    .stream()
```

### BigQuery
```sql
-- Partitioned table (always partition large tables)
CREATE TABLE myproject.dataset.events (
  event_id STRING,
  event_type STRING,
  created_at TIMESTAMP,
  payload JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY event_type
OPTIONS (
  partition_expiration_days=365,
  require_partition_filter=true  -- prevent full scans
);

-- Cost: $6.25/TB scanned. Always SELECT specific columns, never SELECT *
-- Slot reservations for predictable cost at scale
```

### Cloud Spanner
```sql
-- Globally distributed, strongly consistent relational DB
-- Use for: financial systems, inventory, multi-region ACID
-- Cost: ~$0.90/node/hr (minimum 1 node = ~$650/mo)
-- Consider only when: global consistency + 99.999% SLA required
CREATE TABLE Orders (
  OrderId STRING(36) NOT NULL,
  UserId STRING(36) NOT NULL,
  Amount NUMERIC,
  CreatedAt TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (OrderId);

-- Interleaved tables for parent-child locality
CREATE TABLE OrderItems (
  OrderId STRING(36) NOT NULL,
  ItemId STRING(36) NOT NULL,
  ProductId STRING(36),
  Quantity INT64
) PRIMARY KEY (OrderId, ItemId),
  INTERLEAVE IN PARENT Orders ON DELETE CASCADE;
```

## 2.3 Storage & Networking

```bash
# Cloud Storage (equivalent to S3)
gsutil mb -l asia-southeast1 -c standard gs://my-bucket
gsutil lifecycle set lifecycle.json gs://my-bucket
gsutil rsync -r local-dir/ gs://my-bucket/prefix/

# VPC
gcloud compute networks create my-vpc --subnet-mode=custom
gcloud compute networks subnets create private \
  --network=my-vpc --range=10.0.1.0/24 --region=asia-southeast1 \
  --enable-private-ip-google-access  # Access Google APIs without NAT

# Cloud Armor (WAF equivalent)
gcloud compute security-policies create my-policy
gcloud compute security-policies rules create 1000 \
  --security-policy=my-policy --action=deny-403 \
  --expression="evaluatePreconfiguredExpr('sqli-v33-stable')"
gcloud compute security-policies rules create 2000 \
  --security-policy=my-policy --action=throttle \
  --expression="true" --rate-limit-threshold-count=100 \
  --rate-limit-threshold-interval-sec=60
```

## 2.4 IAM & Security

```bash
# Service Account (like IAM Role)
gcloud iam service-accounts create myapp-sa \
  --display-name="MyApp Service Account"

# Workload Identity Federation (replace SA keys with OIDC)
# GitHub Actions -> GCP (no keys!)
gcloud iam workload-identity-pools create github-pool \
  --location=global
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository"

# Secret Manager
gcloud secrets create db-password --replication-policy=user-managed \
  --locations=asia-southeast1
echo -n "supersecret" | gcloud secrets versions add db-password --data-file=-
```

## 2.5 CI/CD & Observability

```yaml
# Cloud Build (cloudbuild.yaml)
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'asia-southeast1-docker.pkg.dev/$PROJECT_ID/repo/app:$SHORT_SHA', '.']
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', 'asia-southeast1-docker.pkg.dev/$PROJECT_ID/repo/app:$SHORT_SHA']
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    args: ['gcloud', 'run', 'deploy', 'myservice',
           '--image=asia-southeast1-docker.pkg.dev/$PROJECT_ID/repo/app:$SHORT_SHA',
           '--region=asia-southeast1']
options:
  machineType: E2_HIGHCPU_8
```

**Cloud Operations Suite (Monitoring/Logging/Trace):**
```bash
# Monitoring (PromQL-compatible MQL)
fetch cloud_run_revision
| metric 'run.googleapis.com/request_latencies'
| align delta(1m)
| every 1m
| group_by [resource.service_name], [percentile(value.request_latencies, 99)]

# Logging
resource.type="cloud_run_revision"
severity>=ERROR
jsonPayload.httpRequest.requestUrl=~"/api/.*"

# Trace: auto-instrumented with OpenTelemetry or Cloud Trace SDK
```

**Our GCP Terraform** (`gcp-terraform`):
- Provider: `google ~> 5.45.2`, region `asia-southeast1`
- Manages: projects, VMs, Cloud SQL, storage, VPC, firewalls, IAM
- Main file: `main-new-project.tf` (5,170 lines)
- Environments: staging, prod

---

# 3. Azure

## 3.1 Compute

### App Service
```bash
# Create App Service (PaaS, like Elastic Beanstalk)
az webapp create --resource-group myRG --plan myPlan \
  --name myapp --runtime "NODE:20-lts"

# Deploy slots (blue/green)
az webapp deployment slot create --resource-group myRG --name myapp --slot staging
az webapp deployment slot swap --resource-group myRG --name myapp \
  --slot staging --target-slot production
```

### Azure Functions
```bash
# Create Function App
az functionapp create --resource-group myRG --storage-account myStorage \
  --consumption-plan-location southeastasia \
  --runtime node --runtime-version 20 --functions-version 4 \
  --name myFuncApp

# Durable Functions for orchestration (like Step Functions)
# Timer trigger (cron)
{
  "bindings": [{
    "name": "timer",
    "type": "timerTrigger",
    "direction": "in",
    "schedule": "0 */5 * * * *"
  }]
}
```

### AKS
```bash
az aks create --resource-group myRG --name myAKS \
  --node-count 3 --node-vm-size Standard_DS2_v2 \
  --enable-managed-identity --enable-addons monitoring \
  --network-plugin azure --network-policy azure \
  --generate-ssh-keys

# Enable KEDA for event-driven autoscaling
az aks update --resource-group myRG --name myAKS --enable-keda
```

## 3.2 Database

### Azure SQL
```bash
az sql server create --resource-group myRG --name myserver \
  --admin-user adminuser --admin-password $PASSWORD \
  --location southeastasia

az sql db create --resource-group myRG --server myserver --name mydb \
  --edition GeneralPurpose --family Gen5 --capacity 2 \
  --zone-redundant true
```

### Cosmos DB
```python
# Multi-model (SQL API, MongoDB API, Cassandra, Gremlin, Table)
# Global distribution, single-digit ms latency
# Cost: Request Units (RU/s) — 400 RU/s minimum

# Partition key selection (critical for performance):
# Good: /userId (high cardinality, even distribution)
# Bad: /country (hot partitions), /createdDate (append-only)

from azure.cosmos import CosmosClient
client = CosmosClient(url, credential)
db = client.get_database_client('mydb')
container = db.get_container_client('items')

container.upsert_item({
    'id': 'item-1',
    'partitionKey': 'user-123',
    'type': 'order',
    'total': 99.99
})
```

## 3.3 Identity & Security

### Entra ID (Azure AD)

```bash
# Managed Identity (like IAM Role — no credentials)
az webapp identity assign --resource-group myRG --name myapp
# Then grant the managed identity access to resources:
az keyvault set-policy --name myVault \
  --object-id <managed-identity-object-id> \
  --secret-permissions get list

# Workload Identity Federation (for CI/CD, like OIDC)
az ad app federated-credential create --id <app-object-id> \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:THINKBITTH/myrepo:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

### Key Vault
```bash
az keyvault create --name myVault --resource-group myRG \
  --location southeastasia --enable-rbac-authorization true

az keyvault secret set --vault-name myVault --name db-password --value "secret"

# Reference in App Service
az webapp config appsettings set --resource-group myRG --name myapp \
  --settings "DB_PASSWORD=@Microsoft.KeyVault(VaultName=myVault;SecretName=db-password)"
```

## 3.4 CI/CD & Monitoring

### Azure DevOps / GitHub Actions Integration
```yaml
# GitHub Actions with OIDC (no secrets)
- uses: azure/login@v2
  with:
    client-id: ${{ secrets.AZURE_CLIENT_ID }}
    tenant-id: ${{ secrets.AZURE_TENANT_ID }}
    subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
```

### Application Insights
```bash
# Auto-instrumentation for Node.js
npm install applicationinsights
# In code:
const appInsights = require('applicationinsights');
appInsights.setup(process.env.APPLICATIONINSIGHTS_CONNECTION_STRING)
  .setAutoCollectRequests(true)
  .setAutoCollectDependencies(true)
  .setAutoCollectExceptions(true)
  .start();

# KQL queries in Log Analytics
requests
| where timestamp > ago(1h)
| summarize count(), avg(duration), percentile(duration, 99) by bin(timestamp, 5m)
| render timechart
```

**Our Azure Infrastructure:**
- `azure-terraform/mfa-portal`: App Service + SQL + KeyVault
- `azure-terraform/tb-ai-mfa-portal-gen2`: Flask/FastAPI + Azure OpenAI GPT-4 pipeline
- Provider: `azurerm ~> 3.117.1`
- Revenue Taxpayer project: Azure Functions + Cosmos DB

---

# 4. Huawei Cloud

## 4.1 AI/ML Services

### ModelArts
```python
# Training job submission
from modelarts.session import Session
from modelarts.estimatorV2 import Estimator

session = Session()
estimator = Estimator(
    framework_type='PyTorch',
    framework_version='2.1.0',
    code_dir='/obs-bucket/code/',
    boot_file='train.py',
    hyperparameters={'epochs': 10, 'batch_size': 32},
    output_path='/obs-bucket/output/',
    train_instance_type='modelarts.p3.large.ex',  # Ascend 910B
    train_instance_count=1
)
estimator.fit(inputs='/obs-bucket/data/')

# Model deployment (inference)
from modelarts.model import Model, Predictor
predictor = Model(model_id='xxx').deploy_predictor(
    instance_type='modelarts.vm.cpu.2u',
    instance_count=1
)
result = predictor.predict(data={'text': 'classify this'})
```

### Ascend NPU (910B/910C)
```python
# MindSpore on Ascend
import mindspore as ms
ms.set_context(device_target="Ascend")

# PyTorch on Ascend (torch_npu)
import torch
import torch_npu
device = torch.device("npu:0")
model = model.to(device)

# Performance comparison:
# Ascend 910B: ~320 TFLOPS FP16 (comparable to A100)
# Ascend 910C: ~640 TFLOPS FP16 (comparable to H100, export-restricted alternative)
# Cost: 30-50% cheaper than NVIDIA equivalents on Huawei Cloud
```

## 4.2 Core Services

```bash
# ECS (Elastic Cloud Server) — like EC2
hcloud ecs create-server --name myserver \
  --flavor s7.large.2 --image Ubuntu22.04 \
  --vpc-id vpc-xxx --subnet-id subnet-xxx

# OBS (Object Storage Service) — like S3
hcloud obs mb obs://my-bucket --location=ap-southeast-3
hcloud obs cp local-file obs://my-bucket/key

# FunctionGraph — like Lambda
hcloud functiongraph create-function --name myFunc \
  --runtime python3.9 --handler index.handler \
  --memory 128 --timeout 30

# CCE (Cloud Container Engine) — Kubernetes
hcloud cce create-cluster --name myCluster \
  --type VirtualMachine --flavor cce.s2.small \
  --vpc-id vpc-xxx --subnet-id subnet-xxx

# GaussDB (distributed PostgreSQL/MySQL compatible)
# DCS Redis (managed Redis, compatible with open-source Redis)
# CSS (Cloud Search Service, Elasticsearch compatible)
```

**Our Huawei Cloud Usage:**
- Project: `surrogate-1` (Custom LLM Fine-tuning with GLM-5)
- Uses: ModelArts, Ascend NPU, OBS

---

# 5. Infrastructure as Code

## 5.1 Terraform

### Provider Configuration
```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }

  backend "s3" {
    bucket         = "thinkbit-terraform-state"
    key            = "service-name/terraform.tfstate"
    region         = "ap-southeast-1"
    dynamodb_table = "terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = "ap-southeast-7"
  default_tags {
    tags = {
      ManagedBy   = "terraform"
      Project     = var.project
      Environment = var.environment
    }
  }
}
```

### Module Design
```hcl
# Module: modules/ecs-service/main.tf
variable "name" { type = string }
variable "image" { type = string }
variable "cpu" { type = number, default = 256 }
variable "memory" { type = number, default = 512 }
variable "desired_count" { type = number, default = 2 }
variable "health_check_path" { type = string, default = "/health" }
variable "secrets" {
  type    = map(string)
  default = {}
  # Usage: { DB_PASSWORD = "arn:aws:secretsmanager:..." }
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [aws_security_group.service.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  lifecycle {
    ignore_changes = [desired_count]  # Let autoscaling manage
  }
}

output "service_name" { value = aws_ecs_service.this.name }
output "target_group_arn" { value = aws_lb_target_group.this.arn }
```

### State Management
```bash
# Move resource between modules
terraform state mv 'module.old.aws_instance.web' 'module.new.aws_instance.web'

# Import existing resource
terraform import 'aws_instance.web' i-1234567890abcdef0

# Remove resource from state (stop managing, don't destroy)
terraform state rm 'aws_instance.web'

# moved blocks (Terraform 1.1+) — declarative refactoring
moved {
  from = aws_instance.web
  to   = module.compute.aws_instance.web
}
```

### for_each vs count
```hcl
# for_each (preferred — stable keys, no index shifting)
variable "services" {
  type = map(object({
    cpu    = number
    memory = number
  }))
  default = {
    "api"    = { cpu = 512, memory = 1024 }
    "worker" = { cpu = 256, memory = 512 }
  }
}

resource "aws_ecs_service" "this" {
  for_each = var.services
  name     = each.key
  # ...
}

# count (only for on/off toggles)
resource "aws_cloudwatch_log_group" "debug" {
  count = var.enable_debug_logging ? 1 : 0
  name  = "/debug/${var.service_name}"
}
```

### Lifecycle Rules
```hcl
lifecycle {
  prevent_destroy = true         # Safety net for databases, S3
  create_before_destroy = true   # Zero-downtime replacements
  ignore_changes = [
    desired_count,               # Managed by autoscaling
    task_definition,             # Managed by CI/CD
    tags["UpdatedAt"],           # External changes
  ]
  replace_triggered_by = [       # Force replace when dependency changes
    aws_secretsmanager_secret_version.db_password
  ]
}
```

### Data Sources
```hcl
# Get latest AMI
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023*-arm64"]
  }
}

# Get current account/region
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Get VPC by tag
data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["main-vpc"]
  }
}
```

### Our Terraform State Backends

| Project | Backend | Bucket/Path |
|---------|---------|-------------|
| thinkbit-devops-iac | S3 | `thinkbit-terraform-state/rds-proxy/terraform.tfstate` |
| gcp-terraform | GCS | backend-new-project.tf |
| azure-terraform | Local | `terraform.tfstate` in project dir |
| AWS-Fix-Assessment | S3 | environments/production/backend.tf |

## 5.2 CloudFormation

### Template Structure
```yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: "ECS Service"

Parameters:
  Environment:
    Type: String
    AllowedValues: [staging, production]
  ServiceName:
    Type: String

Conditions:
  IsProd: !Equals [!Ref Environment, "production"]

Resources:
  Service:
    Type: AWS::ECS::Service
    Properties:
      DesiredCount: !If [IsProd, 3, 1]
      # ...

Outputs:
  ServiceArn:
    Value: !GetAtt Service.Arn
    Export:
      Name: !Sub "${AWS::StackName}-ServiceArn"
```

**Our CF Stack Convention:**
- Naming: `{org}-{project}-{module}[-{environment}]`
- Capabilities: `CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND`
- Params: `[{"ParameterKey":"key","ParameterValue":"value"}]`
- ROLLBACK_COMPLETE: delete stack -> recreate (cannot update)
- Creation order: State Stack -> ECR Stack (ECS only) -> Build Stack -> Deploy Stack -> Service Stack

**Stack Operations:**
```bash
# Create
aws cloudformation create-stack --stack-name excise-wine-nodejs-api-staging \
  --template-body file://template.yaml \
  --parameters file://params.json \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region ap-southeast-7

# Update (always use change sets in prod)
aws cloudformation create-change-set --stack-name mystack \
  --change-set-name update-$(date +%Y%m%d) \
  --template-body file://template.yaml \
  --parameters file://params.json \
  --capabilities CAPABILITY_IAM
aws cloudformation describe-change-set --stack-name mystack \
  --change-set-name update-$(date +%Y%m%d)
# Review changes, then:
aws cloudformation execute-change-set --stack-name mystack \
  --change-set-name update-$(date +%Y%m%d)

# Wait for completion
aws cloudformation wait stack-create-complete --stack-name mystack --region ap-southeast-7
aws cloudformation wait stack-update-complete --stack-name mystack --region ap-southeast-7

# Drift detection
aws cloudformation detect-stack-drift --stack-name mystack
```

**Custom Resources (Lambda-backed):**
```yaml
GitHubRepoCreator:
  Type: Custom::GitHubRepo
  Properties:
    ServiceToken: !GetAtt CreateRepoFunction.Arn
    RepoName: !Ref RepoName
    OrgName: THINKBITTH
```

### Sceptre (CF Orchestration)
```yaml
# config/staging/ecs-service.yaml
template:
  path: templates/ecs-service.yaml
  type: file
parameters:
  Environment: staging
  ServiceName: excise-wine-nodejs-api-staging
dependencies:
  - staging/vpc
  - staging/ecs-cluster
stack_name: excise-wine-nodejs-api-staging
```

## 5.3 CDK

```typescript
import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

// L3 construct (opinionated, high-level)
export class ApiService extends Construct {
  constructor(scope: Construct, id: string, props: ApiServiceProps) {
    super(scope, id);

    const service = new ecs_patterns.ApplicationLoadBalancedFargateService(this, 'Service', {
      cluster: props.cluster,
      cpu: 512,
      memoryLimitMiB: 1024,
      desiredCount: props.isProd ? 3 : 1,
      taskImageOptions: {
        image: ecs.ContainerImage.fromEcrRepository(props.repo, props.imageTag),
        containerPort: 3000,
        secrets: {
          DB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbSecret),
        },
        environment: {
          NODE_ENV: props.environment,
        },
      },
      circuitBreaker: { rollback: true },
    });

    service.targetGroup.configureHealthCheck({
      path: '/health',
      healthyThresholdCount: 2,
      interval: cdk.Duration.seconds(15),
    });
  }
}

// Aspect for tagging compliance
class TaggingAspect implements cdk.IAspect {
  visit(node: IConstruct) {
    if (cdk.TagManager.isTaggable(node)) {
      cdk.Tags.of(node).add('ManagedBy', 'cdk');
      cdk.Tags.of(node).add('Project', 'excise-wine');
    }
  }
}

// Testing
import { Template, Match } from 'aws-cdk-lib/assertions';
test('ECS service created with correct CPU', () => {
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    Cpu: '512',
    Memory: '1024',
  });
});
```

## 5.4 Pulumi

```typescript
import * as aws from "@pulumi/aws";
import * as pulumi from "@pulumi/pulumi";

const config = new pulumi.Config();
const environment = config.require("environment");

const vpc = new aws.ec2.Vpc("main", {
  cidrBlock: "10.0.0.0/16",
  tags: { Name: `${environment}-vpc` },
});

const cluster = new aws.ecs.Cluster("cluster", {
  settings: [{ name: "containerInsights", value: "enabled" }],
});

// Component resource (like Terraform module)
class EcsService extends pulumi.ComponentResource {
  public readonly url: pulumi.Output<string>;

  constructor(name: string, args: EcsServiceArgs, opts?: pulumi.ComponentResourceOptions) {
    super("custom:EcsService", name, {}, opts);
    // ... create task def, service, ALB
    this.url = loadBalancer.dnsName;
  }
}
```

## 5.5 Crossplane

```yaml
# Composition: reusable infra blueprint
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: aws-postgres
spec:
  compositeTypeRef:
    apiVersion: database.example.com/v1alpha1
    kind: PostgreSQL
  resources:
    - name: rds
      base:
        apiVersion: rds.aws.upbound.io/v1beta1
        kind: Instance
        spec:
          forProvider:
            engine: postgres
            engineVersion: "15"
            instanceClass: db.t4g.medium
            allocatedStorage: 20
            publiclyAccessible: false

# Claim: developer self-service
apiVersion: database.example.com/v1alpha1
kind: PostgreSQL
metadata:
  name: myapp-db
  namespace: team-alpha
spec:
  parameters:
    size: small
    version: "15"
```

## 5.6 Ansible

```yaml
# Playbook
- hosts: webservers
  become: yes
  vars:
    app_version: "1.2.3"
  roles:
    - common
    - nginx
    - app

# Role: roles/app/tasks/main.yml
- name: Deploy application
  copy:
    src: "app-{{ app_version }}.tar.gz"
    dest: /opt/app/
  notify: restart app

- name: Configure app
  template:
    src: config.yml.j2
    dest: /opt/app/config.yml
    owner: app
    group: app
    mode: '0640'
  notify: restart app

# handlers/main.yml
- name: restart app
  systemd:
    name: app
    state: restarted
    daemon_reload: yes

# Dynamic inventory (AWS)
# aws_ec2.yml
plugin: aws_ec2
regions: [ap-southeast-7]
filters:
  tag:Environment: staging
keyed_groups:
  - key: tags.Role
    prefix: role
```

---

# 6. Containers & Orchestration

## 6.1 Docker

### Multi-Stage Build (Production)
```dockerfile
# Node.js
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
RUN npm run build

FROM gcr.io/distroless/nodejs20-debian12
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
USER nonroot
EXPOSE 3000
CMD ["dist/server.js"]

# Go
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags="-s -w" -o /app/server ./cmd/server

FROM gcr.io/distroless/static-debian12
COPY --from=builder /app/server /server
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]

# Python
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /deps /usr/local/lib/python3.12/site-packages
COPY . .
USER nobody
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Layer Caching Strategy
```dockerfile
# Order: least changing -> most changing
COPY package*.json ./          # 1. Dependencies (rarely change)
RUN npm ci                      # 2. Install (cached if lock unchanged)
COPY tsconfig.json ./           # 3. Config
COPY src/ ./src/                # 4. Source (changes most)
RUN npm run build               # 5. Build
```

### BuildKit
```bash
# Enable BuildKit
export DOCKER_BUILDKIT=1

# Multi-platform build
docker buildx create --name multiarch --use
docker buildx build --platform linux/amd64,linux/arm64 \
  -t 498952158610.dkr.ecr.ap-southeast-7.amazonaws.com/myapp:v1 \
  --push .

# Cache to registry
docker buildx build \
  --cache-from type=registry,ref=myapp:cache \
  --cache-to type=registry,ref=myapp:cache,mode=max \
  -t myapp:latest .
```

### Security Scanning
```bash
# Trivy (comprehensive, fast)
trivy image --severity HIGH,CRITICAL --exit-code 1 myapp:latest
trivy fs --security-checks vuln,secret,config .

# Docker Scout (Docker native)
docker scout cves myapp:latest
docker scout recommendations myapp:latest

# Snyk
snyk container test myapp:latest --severity-threshold=high
```

### Docker Compose (Development)
```yaml
services:
  api:
    build: .
    ports: ["3000:3000"]
    environment:
      - DATABASE_URL=postgres://user:pass@db:5432/app
      - REDIS_URL=redis://cache:6379
    depends_on:
      db: { condition: service_healthy }
      cache: { condition: service_started }
    volumes:
      - ./src:/app/src  # Hot reload
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: user
      POSTGRES_PASSWORD: pass
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U user -d app"]
      interval: 5s

  cache:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

volumes:
  pgdata:
```

## 6.2 Kubernetes

### Core Resources
```yaml
# Deployment with best practices
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  labels:
    app: api
    version: v1
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0  # Zero-downtime
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
        version: v1
    spec:
      serviceAccountName: api-sa
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: api
          image: myregistry/api:1.0.42
          ports:
            - containerPort: 3000
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          livenessProbe:
            httpGet: { path: /healthz, port: 3000 }
            initialDelaySeconds: 15
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet: { path: /ready, port: 3000 }
            initialDelaySeconds: 5
            periodSeconds: 5
          startupProbe:
            httpGet: { path: /healthz, port: 3000 }
            failureThreshold: 30
            periodSeconds: 2
          env:
            - name: NODE_ENV
              value: production
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: db-creds
                  key: password
          volumeMounts:
            - name: config
              mountPath: /app/config
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: api-config
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfied: DoNotSchedule
          labelSelector:
            matchLabels:
              app: api
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: api
                topologyKey: kubernetes.io/hostname

---
# Service
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 3000
  type: ClusterIP

---
# Ingress (with TLS)
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/rate-limit-rps: "100"
spec:
  tls:
    - hosts: [api.example.com]
      secretName: api-tls
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: api
                port: { number: 80 }

---
# HPA
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 70 }
    - type: Resource
      resource:
        name: memory
        target: { type: Utilization, averageUtilization: 80 }
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 10
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15

---
# PodDisruptionBudget
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api
spec:
  minAvailable: 2  # or maxUnavailable: 1
  selector:
    matchLabels:
      app: api

---
# NetworkPolicy (zero-trust)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api
spec:
  podSelector:
    matchLabels:
      app: api
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: frontend
        - namespaceSelector:
            matchLabels:
              name: ingress-nginx
      ports:
        - port: 3000
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: postgres
      ports:
        - port: 5432
    - to:  # DNS
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - port: 53
          protocol: UDP

---
# RBAC
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: api-role
  namespace: production
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: api-binding
subjects:
  - kind: ServiceAccount
    name: api-sa
roleRef:
  kind: Role
  name: api-role
  apiGroup: rbac.authorization.k8s.io
```

### VPA (Vertical Pod Autoscaler)
```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: api
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  updatePolicy:
    updateMode: "Auto"  # or "Off" for recommendations only
  resourcePolicy:
    containerPolicies:
      - containerName: api
        minAllowed:
          cpu: 100m
          memory: 128Mi
        maxAllowed:
          cpu: 2
          memory: 4Gi
```

## 6.3 Helm

```yaml
# Chart.yaml
apiVersion: v2
name: api-service
version: 1.0.0
appVersion: "1.0.42"
dependencies:
  - name: postgresql
    version: "13.x.x"
    repository: "https://charts.bitnami.com/bitnami"
    condition: postgresql.enabled

# values.yaml
replicaCount: 3
image:
  repository: myregistry/api
  tag: "1.0.42"
  pullPolicy: IfNotPresent

resources:
  requests: { cpu: 250m, memory: 256Mi }
  limits: { cpu: 500m, memory: 512Mi }

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: api.example.com
      paths: [{path: /, pathType: Prefix}]
  tls:
    - secretName: api-tls
      hosts: [api.example.com]

# Helm hooks
# templates/job-migration.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: "{{ .Release.Name }}-migration"
  annotations:
    "helm.sh/hook": pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
```

```bash
# Common Helm commands
helm upgrade --install api ./chart -f values-staging.yaml -n staging --create-namespace
helm rollback api 3 -n staging
helm template api ./chart -f values-staging.yaml  # Dry-run render
helm diff upgrade api ./chart -f values-staging.yaml  # Plugin: helm-diff
```

## 6.4 Service Mesh

**When to use:**
- mTLS between services (zero-trust networking)
- Advanced traffic management (canary, fault injection, retries)
- Observability (distributed tracing without code changes)
- Authorization policies

**Istio (full-featured, complex):**
```yaml
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: api
spec:
  hosts: [api]
  http:
    - match:
        - headers:
            x-canary: { exact: "true" }
      route:
        - destination: { host: api, subset: canary }
    - route:
        - destination: { host: api, subset: stable }
          weight: 95
        - destination: { host: api, subset: canary }
          weight: 5
```

**Linkerd (lightweight, simpler):**
- Automatic mTLS, latency-aware load balancing
- Lower resource overhead than Istio
- Good for: smaller clusters, simpler requirements

**Cilium (eBPF-based):**
- Network policies at kernel level (faster than iptables)
- Observability without sidecars
- Good for: high-performance, security-focused environments

## 6.5 Container Registries

```bash
# ECR login + push
aws ecr get-login-password --region ap-southeast-7 | \
  docker login --username AWS --password-stdin 498952158610.dkr.ecr.ap-southeast-7.amazonaws.com

# ECR lifecycle policy (keep last 10)
aws ecr put-lifecycle-policy --repository-name myapp --lifecycle-policy-text '{
  "rules": [{"rulePriority": 1, "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 10}, "action": {"type": "expire"}}]
}'

# GHCR (GitHub Container Registry)
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
docker push ghcr.io/THINKBITTH/myapp:v1.0
```

---

# 7. CI/CD Pipelines

## 7.1 GitHub Actions

### Reusable Workflow
```yaml
# .github/workflows/reusable-deploy.yml
name: Deploy
on:
  workflow_call:
    inputs:
      environment: { required: true, type: string }
      image-tag: { required: true, type: string }
    secrets:
      AWS_ROLE_ARN: { required: true }

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    environment: ${{ inputs.environment }}
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ap-southeast-7

      - uses: aws-actions/amazon-ecs-deploy-task-definition@v2
        with:
          task-definition: task-def.json
          service: api-${{ inputs.environment }}
          cluster: excise-cluster
          wait-for-service-stability: true
```

### Composite Action
```yaml
# .github/actions/devsecops/action.yml
name: DevSecOps Scan
inputs:
  severity: { default: 'CRITICAL,HIGH' }
runs:
  using: composite
  steps:
    - name: Secret scan
      uses: gitleaks/gitleaks-action@v2
      continue-on-error: true

    - name: SAST
      uses: returntocorp/semgrep-action@v1
      with:
        config: auto
      continue-on-error: true

    - name: Container scan
      uses: aquasecurity/trivy-action@master
      with:
        scan-type: fs
        severity: ${{ inputs.severity }}
        exit-code: 1
```

### Matrix Build
```yaml
strategy:
  fail-fast: false
  matrix:
    os: [ubuntu-latest, macos-latest]
    node: [18, 20, 22]
    exclude:
      - os: macos-latest
        node: 18
    include:
      - os: ubuntu-latest
        node: 20
        coverage: true

steps:
  - uses: actions/setup-node@v4
    with:
      node-version: ${{ matrix.node }}
      cache: npm

  - run: npm ci && npm test
  - if: matrix.coverage
    run: npm run coverage
```

### OIDC Auth (no long-lived credentials)
```yaml
permissions:
  id-token: write
  contents: read

steps:
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::498952158610:role/GitHubActionsRole
      aws-region: ap-southeast-1
      # No access key/secret needed — OIDC federation
```

### Caching Strategies
```yaml
# npm
- uses: actions/cache@v4
  with:
    path: ~/.npm
    key: npm-${{ hashFiles('**/package-lock.json') }}
    restore-keys: npm-

# Docker layers
- uses: docker/build-push-action@v6
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max

# Terraform
- uses: actions/cache@v4
  with:
    path: |
      ~/.terraform.d/plugin-cache
      .terraform/providers
    key: tf-${{ hashFiles('**/.terraform.lock.hcl') }}
```

## 7.2 Jenkins

```groovy
// Declarative pipeline with shared library
@Library('shared-lib') _
pipeline {
    agent { kubernetes { yamlFile 'jenkins-agent.yaml' } }
    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
        disableConcurrentBuilds()
    }
    environment {
        REGISTRY = credentials('ecr-creds')
    }
    stages {
        stage('Build & Test') {
            parallel {
                stage('Lint') { steps { sh 'npm run lint' } }
                stage('Test') { steps { sh 'npm test' } }
                stage('Security') { steps { sh 'trivy fs .' } }
            }
        }
        stage('Deploy Staging') {
            when { branch 'main' }
            steps { deploy('staging') }
        }
        stage('Approval') {
            when { branch 'main' }
            steps { input message: 'Deploy to production?', ok: 'Deploy' }
        }
        stage('Deploy Prod') {
            when { branch 'main' }
            steps { deploy('production') }
        }
    }
    post {
        failure { slackSend(channel: '#alerts', message: "Build failed: ${env.BUILD_URL}") }
    }
}
```

**Our Jenkins:** `thinkbit-devops-jenkins` repo

## 7.3 GitLab CI

```yaml
stages: [test, build, deploy]

variables:
  DOCKER_BUILDKIT: 1

.deploy_template: &deploy
  image: bitnami/kubectl:latest
  before_script:
    - kubectl config use-context $KUBE_CONTEXT

test:
  stage: test
  image: node:20-alpine
  cache:
    key: ${CI_COMMIT_REF_SLUG}
    paths: [node_modules/]
  script:
    - npm ci
    - npm run lint
    - npm test
  coverage: '/All files\s*\|\s*([\d.]+)/'

build:
  stage: build
  image: docker:24-dind
  services: [docker:24-dind]
  script:
    - docker build -t $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
  rules:
    - if: $CI_COMMIT_BRANCH == "main"

deploy-staging:
  stage: deploy
  <<: *deploy
  environment:
    name: staging
    url: https://staging.example.com
  script:
    - kubectl set image deployment/api api=$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA -n staging
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
```

## 7.4 ArgoCD (GitOps)

```yaml
# App of Apps pattern
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: apps
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/THINKBITTH/k8s-apps
    path: apps
    targetRevision: HEAD
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m

---
# Sync waves (ordered deployment)
# Wave -1: namespace, RBAC
# Wave 0: configmaps, secrets
# Wave 1: database migrations (Job)
# Wave 2: deployments, services
# Wave 3: ingress, HPA
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "1"
```

**Progressive Delivery with Argo Rollouts:**
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api
spec:
  strategy:
    canary:
      steps:
        - setWeight: 5
        - pause: { duration: 5m }
        - analysis:
            templates: [{ templateName: success-rate }]
        - setWeight: 20
        - pause: { duration: 5m }
        - setWeight: 50
        - pause: { duration: 10m }
        - setWeight: 100
      canaryService: api-canary
      stableService: api-stable
      trafficRouting:
        nginx:
          stableIngress: api-ingress

---
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
spec:
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            sum(rate(http_requests_total{status=~"2..",app="api",version="canary"}[5m]))
            /
            sum(rate(http_requests_total{app="api",version="canary"}[5m]))
```

## 7.5 Pipeline Patterns

### Our DevSecOps Pipeline Standard
```
gitleaks (secret scan, warn)
  -> semgrep (SAST, warn/block)
  -> cfn-lint (IaC lint, warn)
  -> SCA: npm audit / govulncheck / pip-audit (block critical)
  -> lint (block)
  -> test (block)
  -> build
  -> checksum (SHA256)
  -> build-info.json
  -> cross-region sync (FATAL on failure)
  -> deploy trigger (deployment-package.zip -> EventBridge -> CodeBuild -> CF)
```

### Versioning Convention
```
Format: {MAJOR}.{MINOR}.{BUILD_NUMBER}
BUILD_NUMBER: auto-incremented via SSM Parameter Store
Immutable artifacts: every build gets unique version
latest symlink for convenience, versioned path for rollback
```

### Deployment Strategy Decision Tree
```
Zero-downtime needed?
├── Yes -> Stateless?
│   ├── Yes -> Blue/Green or Canary
│   │   ├── High confidence? -> Rolling update
│   │   ├── New feature risk? -> Canary (5% -> 20% -> 50% -> 100%)
│   │   └── Database schema change? -> Blue/Green with DB migration
│   └── No (stateful) -> Rolling with PDB + drain
└── No -> In-place with maintenance window
```

### Feature Flag Integration
```
Deploy != Release
1. Deploy new code (dark launch) — behind feature flag
2. Enable flag for internal users (dogfood)
3. Enable flag for 5% (canary)
4. Monitor metrics — error rate, latency, conversion
5. Ramp to 100%
6. Remove flag + dead code (within 2 sprints)

Tools: LaunchDarkly, Unleash, Flagsmith, AWS AppConfig
```

---

# 8. DevSecOps & Security

## 8.1 SAST (Static Application Security Testing)

### SonarQube
```bash
sonar-scanner \
  -Dsonar.projectKey=excise-wine-api \
  -Dsonar.sources=src \
  -Dsonar.tests=test \
  -Dsonar.host.url=http://sonarqube:9000 \
  -Dsonar.token=$SONAR_TOKEN \
  -Dsonar.qualitygate.wait=true

# Quality Gate: coverage >= 80%, no new bugs, no new vulnerabilities
# Catches: code smells, bugs, security hotspots, complexity
```

### Semgrep
```bash
# Auto-config (recommended rules)
semgrep --config auto --error --quiet .

# Custom rules
# .semgrep.yml
rules:
  - id: no-eval
    patterns:
      - pattern: eval(...)
    message: "eval() is dangerous, use a safer alternative"
    severity: ERROR
    languages: [javascript, typescript]

  - id: no-sql-injection
    patterns:
      - pattern: |
          $DB.query(`... ${$INPUT} ...`)
    message: "SQL injection risk — use parameterized queries"
    severity: ERROR
    languages: [javascript, typescript]

# Known issue: semgrep-core binary often missing on Amazon Linux 2 CodeBuild
# Pattern: if command -v semgrep && semgrep --version; then scan; else warn; fi
```

### CodeQL
```yaml
# GitHub Actions
- uses: github/codeql-action/init@v3
  with:
    languages: javascript, python
    queries: security-extended
- uses: github/codeql-action/analyze@v3
# Catches: injection, XSS, path traversal, crypto issues, SSRF
```

## 8.2 DAST (Dynamic Application Security Testing)

```bash
# OWASP ZAP (automated scan)
docker run -t owasp/zap2docker-stable zap-baseline.py \
  -t https://staging.example.com \
  -r report.html \
  -I  # Only fail on HIGH/CRITICAL

# ZAP full scan (slower, more thorough)
docker run -t owasp/zap2docker-stable zap-full-scan.py \
  -t https://staging.example.com \
  -r full-report.html
```

## 8.3 SCA (Software Composition Analysis)

```bash
# npm audit (our standard)
npm audit --omit=dev --audit-level=critical
# --omit=dev: exclude dev dependencies
# npm audit fix (without --force): safe, non-breaking fixes only

# Go
govulncheck ./...

# Python
pip-audit -r requirements.txt

# Snyk (comprehensive)
snyk test --severity-threshold=high
snyk container test myimage:latest

# Trivy (all-in-one)
trivy fs --scanners vuln,secret,misconfig .
trivy image --severity HIGH,CRITICAL myimage:latest

# Dependabot (GitHub native)
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: npm
    directory: "/"
    schedule: { interval: weekly }
    open-pull-requests-limit: 10
    ignore:
      - dependency-name: "*"
        update-types: ["version-update:semver-major"]
```

## 8.4 Secret Scanning

```bash
# Gitleaks (our standard, runs in CI)
gitleaks detect --source . --no-banner --report-format json --report-path gitleaks.json
# Custom config:
# .gitleaks.toml
[allowlist]
  paths = ["test/", "*.test.ts"]

# truffleHog (history scan)
trufflehog git file://. --since-commit HEAD~50 --only-verified

# Pre-commit hook
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
```

## 8.5 Container Security

```bash
# Rootless containers (always)
USER nonroot:nonroot  # in Dockerfile

# Read-only filesystem
docker run --read-only --tmpfs /tmp myapp:latest

# No new privileges
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  capabilities:
    drop: [ALL]
  seccompProfile:
    type: RuntimeDefault

# Image signing (cosign)
cosign sign --key cosign.key myregistry/myapp:v1
cosign verify --key cosign.pub myregistry/myapp:v1

# Admission controller (enforce policies)
# Kyverno policy: require non-root
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-non-root
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-non-root
      match:
        any:
          - resources: { kinds: [Pod] }
      validate:
        message: "Containers must run as non-root"
        pattern:
          spec:
            containers:
              - securityContext:
                  runAsNonRoot: true
```

## 8.6 Threat Modeling

### STRIDE
```
Spoofing        -> Authentication (MFA, certificate-based, OIDC)
Tampering       -> Integrity (checksums, digital signatures, immutable infra)
Repudiation     -> Non-repudiation (audit logs, CloudTrail, signed events)
Info Disclosure -> Confidentiality (encryption at rest/transit, least privilege)
Denial of Svc   -> Availability (rate limiting, WAF, auto-scaling, DDoS protection)
Elevation       -> Authorization (RBAC, SCPs, security boundaries, no root)
```

### Attack Surface Mapping
```
External:
  - ALB (public) -> WAF -> ECS services
  - CloudFront -> S3 (static), ALB (API)
  - API Gateway -> Lambda
  - Cognito endpoints (auth)

Internal:
  - ECS <-> RDS (private subnets, SG restricted)
  - ECS <-> ElastiCache (private subnets)
  - Lambda <-> Secrets Manager (VPC endpoint)
  - CodeBuild <-> S3, ECR (IAM roles)

Supply Chain:
  - npm packages (SCA scanning)
  - Docker base images (vulnerability scanning)
  - GitHub Actions (pin to SHA, not tags)
  - Third-party APIs (circuit breaker, timeout)
```

## 8.7 Compliance

### CIS AWS Foundations Benchmark (Key Controls)

| Control | Implementation |
|---------|---------------|
| 1.1 Root MFA | Hardware FIDO2 key |
| 1.4 No root access keys | Delete all root keys |
| 1.5 MFA for console users | Enforce via IAM policy |
| 2.1 CloudTrail enabled all regions | Organization trail |
| 2.6 S3 access logging | Enable for sensitive buckets |
| 3.1 CloudWatch log metric filter | Unauthorized API calls alarm |
| 4.1 No SG with 0.0.0.0/0 to 22 | Config rule enforcement |
| 5.1 VPC Flow Logs | Enable per VPC |

### Prowler (Automated Compliance)
```bash
# Run full assessment
prowler aws --region ap-southeast-7 --output-formats json,html
prowler aws --compliance cis_2.0_aws --region ap-southeast-7

# Our findings (from AWS-Fix-Assessment, Apr 2026):
# Total: 5,759 findings (down from 6,683)
# FAIL: 1,964 (down from 2,353, -16.5%)
# 203 unique checks, 67% complete
```

### SOC2 Controls Mapping
```
CC6.1 (Logical Access) -> IAM roles, MFA, SSO, access reviews
CC6.2 (System Boundaries) -> VPC, SGs, NACLs, WAF
CC6.3 (Encryption) -> KMS, TLS, S3 encryption, RDS encryption
CC7.1 (Change Management) -> CI/CD pipeline, approval gates, audit trail
CC7.2 (Monitoring) -> CloudWatch, GuardDuty, Security Hub
CC8.1 (Incident Response) -> Runbooks, on-call, postmortem process
```

---

# 9. SRE Practices

## 9.1 SLOs, SLIs, SLAs

### Definition Framework
```
SLI (Service Level Indicator):
  - Availability: successful_requests / total_requests
  - Latency: proportion of requests < threshold
  - Freshness: proportion of data updated within threshold
  - Correctness: proportion of valid responses

SLO (Service Level Objective):
  - Target: SLI >= threshold over time window
  - Example: 99.9% availability over 30 days (43.8 min downtime)

SLA (Service Level Agreement):
  - External contract with consequences
  - Always less strict than SLO (SLO = 99.95%, SLA = 99.9%)

Error Budget = 1 - SLO
  - 99.9% = 43.2 min/month
  - 99.95% = 21.6 min/month
  - 99.99% = 4.32 min/month
```

### Error Budget Policy
```
Budget > 50% remaining: Deploy freely, experiment
Budget 25-50%: Careful deployments, review changes
Budget 10-25%: Emergency only, no experiments
Budget < 10%: Feature freeze, reliability work only
Budget exhausted: All hands on reliability until recovered
```

### Burn Rate Alerts
```yaml
# Prometheus alerting rules
groups:
  - name: slo-alerts
    rules:
      # 1h burn rate (fast burn — pages immediately)
      - alert: HighErrorBurnRate1h
        expr: |
          (
            sum(rate(http_requests_total{status=~"5.."}[1h]))
            / sum(rate(http_requests_total[1h]))
          ) > (14.4 * (1 - 0.999))
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "Burning through error budget 14.4x faster than normal"

      # 6h burn rate (slow burn — tickets)
      - alert: HighErrorBurnRate6h
        expr: |
          (
            sum(rate(http_requests_total{status=~"5.."}[6h]))
            / sum(rate(http_requests_total[6h]))
          ) > (6 * (1 - 0.999))
        for: 5m
        labels: { severity: warning }
```

## 9.2 Incident Management

### Severity Levels

| Level | Impact | Response | Example |
|-------|--------|----------|---------|
| SEV1 | Complete outage, data loss risk | Page immediately, war room | All APIs down, DB corruption |
| SEV2 | Major degradation, >10% users | Page, 15 min response | High error rate, auth broken |
| SEV3 | Minor degradation, <10% users | On-call, 1 hr response | Slow queries, one endpoint down |
| SEV4 | Cosmetic, no user impact | Next business day | Dashboard broken, log noise |

### Incident Lifecycle
```
1. DETECT    -> Monitoring alert or user report
2. TRIAGE    -> Assess severity, page if needed
3. ASSEMBLE  -> Incident commander, comms lead, responders
4. MITIGATE  -> Rollback, scale up, failover, hotfix
5. RESOLVE   -> Root cause fixed, systems stable
6. POSTMORTEM -> Within 48 hours, blameless, action items
```

### War Room Protocol
```
Incident Commander (IC):
  - Owns the incident end-to-end
  - Delegates tasks, makes decisions
  - Never does the debugging (coordinates only)
  - Updates stakeholders every 15-30 min

Communications Lead:
  - Status page updates
  - Stakeholder notifications
  - Shields responders from interruptions

Responders:
  - Debug, implement fixes
  - Report findings to IC
  - Document actions in incident channel
```

## 9.3 Chaos Engineering

### Principles
```
1. Define steady state (normal metrics)
2. Hypothesize: "Service continues working when X fails"
3. Inject failure in production (start small)
4. Observe behavior vs hypothesis
5. Fix or improve resilience
```

### Tools & Experiments
```bash
# AWS FIS (Fault Injection Simulator)
aws fis create-experiment-template --description "Kill 30% of ECS tasks" \
  --targets '{"ecsService": {"resourceType": "aws:ecs:service", "resourceArns": ["..."]}}' \
  --actions '{"stopTasks": {"actionId": "aws:ecs:stop-task", "targets": {"Services": "ecsService"}, "parameters": {"percent": "30"}}}'

# Litmus Chaos (Kubernetes)
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: pod-kill
spec:
  engineState: active
  appinfo:
    appns: production
    applabel: app=api
  chaosServiceAccount: litmus-admin
  experiments:
    - name: pod-delete
      spec:
        components:
          env:
            - name: TOTAL_CHAOS_DURATION
              value: "30"
            - name: CHAOS_INTERVAL
              value: "10"

# Simple chaos scripts
# Network latency injection
tc qdisc add dev eth0 root netem delay 200ms 50ms
# Disk fill
fallocate -l 10G /tmp/fill
# CPU stress
stress-ng --cpu 4 --timeout 60s
# Memory pressure
stress-ng --vm 2 --vm-bytes 80% --timeout 60s
```

### Game Day Checklist
```
Before:
  [ ] Define scope (which services, which failures)
  [ ] Set blast radius limits (one AZ, one service)
  [ ] Ensure rollback plan exists
  [ ] Notify on-call and stakeholders
  [ ] Have monitoring dashboards ready
  [ ] Record baseline metrics

During:
  [ ] Inject failure
  [ ] Monitor impact (latency, errors, throughput)
  [ ] Verify alerts fire
  [ ] Verify runbooks are accurate
  [ ] Time to detection (TTD)
  [ ] Time to mitigation (TTM)

After:
  [ ] Document findings
  [ ] Create tickets for improvements
  [ ] Update runbooks
  [ ] Share learnings
```

## 9.4 Capacity Planning

### Load Testing Tools

```bash
# k6 (modern, scriptable, cloud-native)
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '2m', target: 100 },   // Ramp up
    { duration: '5m', target: 100 },   // Steady state
    { duration: '2m', target: 300 },   // Stress
    { duration: '5m', target: 300 },   // Sustained stress
    { duration: '2m', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(99)<500', 'p(95)<200'],
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const res = http.get('https://staging.example.com/api/health');
  check(res, {
    'status is 200': (r) => r.status === 200,
    'latency < 200ms': (r) => r.timings.duration < 200,
  });
  sleep(1);
}

# Run: k6 run --out influxdb=http://localhost:8086/k6 load-test.js

# Locust (Python-based)
from locust import HttpUser, task, between
class ApiUser(HttpUser):
    wait_time = between(1, 3)

    @task(3)
    def get_items(self):
        self.client.get("/api/items")

    @task(1)
    def create_item(self):
        self.client.post("/api/items", json={"name": "test"})
```

### Capacity Formula
```
Required capacity = (Peak RPS * Response Time) / Utilization Target
  + Headroom (20-30%)
  + Failure tolerance (N+1 or N+2)

Example:
  Peak: 1000 RPS
  Response: 100ms
  Target utilization: 70%
  Concurrent requests: 1000 * 0.1 = 100
  Required capacity: 100 / 0.7 = 143 concurrent connections
  With N+1 redundancy: 143 * 1.5 = 215
  At 50 connections/instance: ceil(215/50) = 5 instances
```

## 9.5 Postmortem Template

```markdown
## Incident: [Title]
**Date**: YYYY-MM-DD | **Duration**: Xh Ym | **Severity**: SEV-N
**Impact**: N users affected, $X revenue impact
**Incident Commander**: @name

### Timeline (UTC+7)
| Time | Event |
|------|-------|
| 14:00 | Deploy v1.2.3 to production |
| 14:15 | Error rate alert fires (5% -> 25%) |
| 14:18 | IC acknowledged, war room opened |
| 14:25 | Root cause identified: DB connection pool exhausted |
| 14:30 | Rollback initiated to v1.2.2 |
| 14:35 | Error rate normalized |
| 14:45 | Incident resolved |

### Root Cause
New feature increased query count per request from 3 to 12.
Connection pool (max 20) exhausted under load.

### 5 Whys
1. Why did errors spike? -> DB connections exhausted
2. Why were connections exhausted? -> 4x more queries per request
3. Why 4x more queries? -> New feature did N+1 queries
4. Why wasn't this caught? -> Load test only ran at 10% production traffic
5. Why 10%? -> No standard for load test scale

### Action Items
| # | Action | Owner | Priority | Status |
|---|--------|-------|----------|--------|
| 1 | Add DataLoader to batch queries | @dev | P0 | Done |
| 2 | Increase pool to 100, add monitoring | @ops | P0 | Done |
| 3 | Load test at 100% production traffic | @qa | P1 | In progress |
| 4 | Alert on connection pool utilization > 80% | @ops | P1 | Done |
| 5 | Add N+1 query detection to CI | @dev | P2 | Todo |

### Lessons Learned
- Load tests must simulate production scale
- Connection pool exhaustion needs dedicated monitoring
- N+1 queries are a systemic risk we need automated detection for
```

## 9.6 Toil Reduction

### Toil Classification
```
Toil = manual, repetitive, automatable, reactive, no enduring value

Toil budget: max 50% of team time (Google SRE standard)
Track with: toil surveys, ticket labels, time tracking

Priority Matrix:
  High toil + Easy to automate = DO FIRST
  High toil + Hard to automate = PLAN AND INVEST
  Low toil + Easy to automate = BATCH AND AUTOMATE
  Low toil + Hard to automate = DEFER
```

## 9.7 Rollback Strategies

### Our Service-Specific Rollback

| Service Type | Rollback Method | Time |
|-------------|----------------|------|
| Lambda | Update to previous S3 zip version via CF | 2-5 min |
| ECS | Update service to previous task definition revision | 3-10 min |
| EC2 (CodeDeploy) | Rollback deployment via CodeDeploy | 5-15 min |
| CloudFormation | Rollback to previous stack version | 5-20 min |
| Database | Restore from snapshot (last resort) | 30-60 min |

```bash
# ECS rollback
PREV_TASK=$(aws ecs describe-services --cluster excise-wine --services nodejs-api \
  --query 'services[0].taskDefinition' --output text | sed 's/:.*/:/' )
PREV_REV=$(($(echo $PREV_TASK | grep -o ':[0-9]*$' | tr -d ':') - 1))
aws ecs update-service --cluster excise-wine --service nodejs-api \
  --task-definition "${PREV_TASK}${PREV_REV}" --force-new-deployment

# Lambda rollback
aws lambda update-function-code --function-name myFunc \
  --s3-bucket thinkbit-devops-artifacts-apse7 \
  --s3-key lambda/excise-wine-authen-staging/1.0.41/function.zip
```

---

# 10. Platform Engineering

## 10.1 Internal Developer Platform (IDP)

### Architecture
```
Developers
  |
  v
Self-Service Portal (Backstage / Port)
  |
  v
Platform API (Golden Paths, Templates)
  |
  v
Infrastructure (IaC, K8s, Cloud APIs)
  |
  v
Observability (Metrics, Logs, Traces)
```

### Backstage
```yaml
# Template: scaffold new service
apiVersion: scaffolder.backstage.io/v1beta3
kind: Template
metadata:
  name: nodejs-api
  title: Node.js API Service
spec:
  owner: platform-team
  type: service
  parameters:
    - title: Service Info
      required: [name, owner]
      properties:
        name: { type: string, description: "Service name" }
        owner: { type: string, ui:field: OwnerPicker }
        description: { type: string }
    - title: Infrastructure
      properties:
        database: { type: string, enum: [none, postgresql, dynamodb] }
        cache: { type: string, enum: [none, redis] }
  steps:
    - id: fetch-template
      action: fetch:template
      input:
        url: ./skeleton
        values: { name: ${{ parameters.name }} }
    - id: publish
      action: publish:github
      input:
        repoUrl: github.com?owner=THINKBITTH&repo=${{ parameters.name }}
    - id: register
      action: catalog:register
```

### Port (Port.io)
```yaml
# Blueprint: define entity model
identifier: microservice
title: Microservice
schema:
  properties:
    language: { type: string, enum: [node, go, python] }
    owner: { type: string, format: team }
    lifecycle: { type: string, enum: [development, staging, production] }
    health_url: { type: string, format: url }
  required: [language, owner, lifecycle]
# Self-service actions trigger GitHub Actions / ArgoCD
```

## 10.2 DORA Metrics

| Metric | Elite | High | Medium | Low |
|--------|-------|------|--------|-----|
| Deployment Frequency | On-demand (multiple/day) | Weekly-Monthly | Monthly-Biannually | Biannually+ |
| Lead Time for Changes | <1 hour | 1 day-1 week | 1 week-1 month | 1-6 months |
| Mean Time to Restore (MTTR) | <1 hour | <1 day | 1 day-1 week | 1 week+ |
| Change Failure Rate | 0-15% | 16-30% | 16-30% | 46-60% |

```bash
# Measure deployment frequency
git log --oneline --since="30 days ago" --merges | wc -l

# Measure lead time (commit to production)
# Track: PR merge time + deployment pipeline time + approval time

# Measure MTTR
# Track: alert time -> resolution time from incident management system

# Measure change failure rate
# Track: rollback/hotfix deployments / total deployments
```

## 10.3 Golden Paths

```
Golden Path = Opinionated, supported way to do common tasks

Examples:
  - New Service:     Backstage template -> GitHub repo -> CI/CD -> ECS/Lambda
  - Database:        Self-service Crossplane claim -> RDS/DynamoDB
  - Secrets:         Self-service -> Secrets Manager -> injected at runtime
  - Monitoring:      Standard dashboards auto-provisioned with service
  - On-boarding:     New dev -> access request -> auto-provisioned

Guardrails (not gates):
  - Linting rules catch anti-patterns (not block deploys)
  - Recommended patterns in templates (easy to follow)
  - Security scanning warns, blocks only critical
  - Cost alerts, not cost gates (except budget limits)
```

## 10.4 API Platform Patterns

```yaml
# Rate limiting (API Gateway)
UsagePlan:
  Type: AWS::ApiGateway::UsagePlan
  Properties:
    Throttle:
      RateLimit: 100     # requests/second
      BurstLimit: 200    # concurrent
    Quota:
      Limit: 10000       # requests/day
      Period: DAY

# API Key management
# - Issue per consumer/team
# - Track usage per key
# - Revoke without downtime
# - Separate keys per environment
```

---

# 11. Observability

## 11.1 Metrics

### Prometheus
```yaml
# Recording rules (pre-compute expensive queries)
groups:
  - name: api-metrics
    interval: 30s
    rules:
      - record: job:http_requests:rate5m
        expr: sum(rate(http_requests_total[5m])) by (job)

      - record: job:http_request_duration:p99
        expr: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, job))

      - record: job:http_errors:rate5m
        expr: sum(rate(http_requests_total{status=~"5.."}[5m])) by (job)
```

**Essential PromQL:**
```promql
# Request rate
sum(rate(http_requests_total[5m])) by (service)

# Error rate percentage
sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)
/ sum(rate(http_requests_total[5m])) by (service) * 100

# p99 latency
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))

# Saturation (CPU)
1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) by (instance)

# Memory usage percentage
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100

# Disk usage
1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}

# Top 5 consumers
topk(5, sum(rate(http_requests_total[5m])) by (path))

# Rate of change (predict)
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 24*3600)
```

### Grafana Dashboard Best Practices
```
Layout per service:
  Row 1: Golden signals (request rate, error rate, latency p50/p95/p99)
  Row 2: Resource utilization (CPU, memory, disk, network)
  Row 3: Dependencies (DB connections, cache hit rate, external API latency)
  Row 4: Business metrics (orders/min, users online, revenue)

Variables:
  $environment: staging, production
  $service: api, worker, cron
  $interval: auto-scaled based on time range

Annotations:
  - Deployments (green vertical line)
  - Incidents (red vertical line)
  - Config changes (yellow vertical line)
```

## 11.2 Logs

### Loki (LogQL)
```logql
# Error logs with context
{namespace="production", app="api"} |= "error" | json | status >= 500

# Rate of errors
sum(rate({app="api"} |= "ERROR" [5m])) by (path)

# Extract and aggregate
{app="api"} | json | latency_ms > 500 | line_format "slow: {{.path}} {{.latency_ms}}ms"

# Pattern match
{app="api"} | pattern "<ip> - - [<timestamp>] \"<method> <path> <_>\" <status> <size>"
  | status = "500"
  | line_format "{{.method}} {{.path}}"
```

### ELK (Elasticsearch)
```json
// Kibana query (KQL)
// Find slow API calls
response.status: 500 AND service.name: "api" AND event.duration > 1000

// Elasticsearch aggregation
{
  "aggs": {
    "error_rate_over_time": {
      "date_histogram": { "field": "@timestamp", "calendar_interval": "5m" },
      "aggs": {
        "errors": { "filter": { "range": { "response.status": { "gte": 500 } } } },
        "error_rate": {
          "bucket_script": {
            "buckets_path": { "errors": "errors._count", "total": "_count" },
            "script": "params.errors / params.total * 100"
          }
        }
      }
    }
  }
}
```

### CloudWatch Logs Insights
```
# Lambda performance analysis
filter @type = "REPORT"
| stats avg(@duration) as avg_ms,
        max(@duration) as max_ms,
        pct(@duration, 99) as p99_ms,
        count(*) as invocations,
        sum(@initDuration > 0) as cold_starts
  by bin(1h)

# ECS error tracking
filter @message like /ERROR/
| parse @message "[*] * - *" as level, component, error_msg
| stats count(*) as error_count by component
| sort error_count desc
| limit 20
```

## 11.3 Traces

### OpenTelemetry
```typescript
// Auto-instrumentation (Node.js)
import { NodeSDK } from '@opentelemetry/sdk-node';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { getNodeAutoInstrumentations } from '@opentelemetry/auto-instrumentations-node';

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'http://otel-collector:4318/v1/traces',
  }),
  instrumentations: [getNodeAutoInstrumentations({
    '@opentelemetry/instrumentation-http': { ignoreIncomingPaths: ['/health'] },
    '@opentelemetry/instrumentation-express': {},
    '@opentelemetry/instrumentation-pg': {},
    '@opentelemetry/instrumentation-redis': {},
  })],
  serviceName: 'excise-wine-nodejs-api',
  resource: { 'deployment.environment': 'staging' },
});
sdk.start();

// Manual spans for business logic
import { trace } from '@opentelemetry/api';
const tracer = trace.getTracer('order-service');

async function processOrder(order: Order) {
  return tracer.startActiveSpan('processOrder', async (span) => {
    span.setAttribute('order.id', order.id);
    span.setAttribute('order.total', order.total);
    try {
      const result = await chargePayment(order);
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      span.recordException(error);
      throw error;
    } finally {
      span.end();
    }
  });
}

// Baggage (propagate context across services)
import { propagation, context } from '@opentelemetry/api';
propagation.setBaggage(context.active(),
  propagation.createBaggage({ 'user.id': { value: userId } })
);
```

### OpenTelemetry Collector
```yaml
# otel-collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  batch:
    timeout: 5s
    send_batch_size: 1024
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
  tail_sampling:
    decision_wait: 10s
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }
      - name: sample-rest
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls: { insecure: true }
  prometheus:
    endpoint: 0.0.0.0:8889
  loki:
    endpoint: http://loki:3100/loki/api/v1/push

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, tail_sampling, batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus]
```

## 11.4 Alerting

### AlertManager Configuration
```yaml
route:
  receiver: default
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - match: { severity: critical }
      receiver: pagerduty
      repeat_interval: 15m
    - match: { severity: warning }
      receiver: slack
      repeat_interval: 4h
    - match: { alertname: DeadManSwitch }
      receiver: deadman
      repeat_interval: 1m

receivers:
  - name: pagerduty
    pagerduty_configs:
      - service_key: <key>
        severity: '{{ .CommonLabels.severity }}'
  - name: slack
    slack_configs:
      - channel: '#ops-alerts'
        title: '{{ .CommonLabels.alertname }}'
        text: '{{ .CommonAnnotations.summary }}'
  - name: deadman
    webhook_configs:
      - url: http://deadmanssnitch.com/xxx

inhibit_rules:
  - source_match: { severity: critical }
    target_match: { severity: warning }
    equal: [alertname, cluster, service]
```

### Alert Quality Rules
```
1. Every alert must be actionable (leads to human action)
2. Every alert must have a runbook link
3. Alert on symptoms (error rate), not causes (disk full)
4. No flapping: use proper for/pending durations
5. Escalation path: Slack (info) -> PagerDuty (critical)
6. Review alert fatigue monthly: if alert fires >5/week with no action, delete it
7. Dead man's switch: alert WHEN monitoring is DOWN
```

## 11.5 Dashboard Methods

### RED Method (per service)
```
Rate:     requests per second
Errors:   errors per second (or error %)
Duration: latency distribution (p50, p95, p99)
```

### USE Method (per resource)
```
Utilization: % time resource is busy (CPU %, memory %)
Saturation:  queue depth, waiting threads
Errors:      error count (disk errors, network drops)
```

### Four Golden Signals (Google SRE)
```
Latency:    Time to serve requests (distinguish success vs error latency)
Traffic:    Demand on the system (requests/sec, sessions)
Errors:     Rate of failed requests (explicit, implicit, policy-based)
Saturation: How "full" the system is (most constrained resource)
```

## 11.6 APM Patterns

```
Datadog:
  - Unified platform: metrics + logs + traces + profiling
  - APM: auto-instrumentation for most languages
  - Pricing: per host ($23-33/host/mo) or per container

New Relic:
  - Free tier: 100GB/mo ingest
  - Full-stack observability
  - NRQL query language (powerful)

Dynatrace:
  - AI-powered root cause analysis (Davis AI)
  - Auto-discovery of topology
  - Best for: large enterprise, complex microservices
  - Pricing: per host ($69/host/mo)
```

**Our Monitoring Stack:**
- CloudWatch for AWS-native metrics and logs
- Prometheus + Mimir for custom metrics
- Grafana for dashboards
- X-Ray for AWS-native tracing

---

# 12. Networking

## 12.1 DNS

### Record Types

| Type | Purpose | Example |
|------|---------|---------|
| A | IPv4 address | `api.example.com -> 1.2.3.4` |
| AAAA | IPv6 address | `api.example.com -> 2001:db8::1` |
| CNAME | Alias (not at zone apex) | `www -> api.example.com` |
| ALIAS/ANAME | Alias at zone apex | `example.com -> d1234.cloudfront.net` |
| MX | Mail server | `example.com -> 10 mail.example.com` |
| TXT | Verification, SPF, DKIM | `v=spf1 include:_spf.google.com ~all` |
| SRV | Service discovery | `_http._tcp.api 10 0 8080 api.example.com` |
| CAA | Certificate authority auth | `0 issue "amazon.com"` |
| NS | Nameserver delegation | `sub.example.com -> ns1.provider.com` |

### TTL Strategy
```
Production records:       300s (5 min) — balance between cache and failover speed
During migration:         60s (reduce TTL 24h before migration, restore after)
Static records (MX, TXT): 3600s (1 hr)
CloudFront/ALB aliases:   60s (AWS managed)
Failover records:         30-60s (fast failover)
```

## 12.2 TLS/SSL

### Certificate Management
```bash
# ACM (AWS - free, auto-renewal)
aws acm request-certificate \
  --domain-name "*.example.com" \
  --subject-alternative-names "example.com" \
  --validation-method DNS
# Add CNAME records for validation, auto-renews

# Let's Encrypt (free, 90-day, auto-renewal with certbot)
certbot certonly --dns-cloudflare \
  -d "*.example.com" -d "example.com" \
  --dns-cloudflare-credentials /root/.cloudflare.ini
# Cron: certbot renew --deploy-hook "systemctl reload nginx"

# Certificate rotation (zero-downtime)
# ALB: update listener certificate, ALB handles gracefully
# Nginx: reload (not restart): nginx -s reload
# K8s: cert-manager auto-rotates via Ingress annotation
```

### mTLS (Mutual TLS)
```
Client -> presents client cert -> Server validates
Server -> presents server cert -> Client validates
Both sides authenticated

Use cases: service-to-service, API clients, IoT devices
Implementation: service mesh (Istio/Linkerd auto-mTLS) or manual (nginx ssl_verify_client on)
```

## 12.3 Load Balancing

### Algorithms

| Algorithm | Best For | Drawback |
|-----------|----------|----------|
| Round Robin | Equal capacity servers | Ignores server load |
| Least Connections | Variable request duration | Slight overhead |
| Weighted Round Robin | Mixed capacity servers | Manual weight tuning |
| IP Hash | Session affinity | Uneven distribution |
| Random with Two Choices | High throughput | Slightly less optimal |

### Health Check Patterns
```yaml
# Deep health check (checks dependencies)
GET /health
{
  "status": "healthy",
  "version": "1.0.42",
  "checks": {
    "database": { "status": "healthy", "latency_ms": 5 },
    "cache": { "status": "healthy", "latency_ms": 1 },
    "external_api": { "status": "degraded", "latency_ms": 500 }
  }
}

# Shallow health check (for load balancer — fast, no deps)
GET /healthz -> 200 OK

# Readiness check (can accept traffic?)
GET /ready -> 200 if ready, 503 if not
```

## 12.4 CDN

### Cache Control Headers
```
# Long-lived assets (hashed filenames)
Cache-Control: public, max-age=31536000, immutable

# API responses (don't cache by default)
Cache-Control: no-store

# HTML pages (revalidate)
Cache-Control: public, max-age=0, must-revalidate
ETag: "abc123"

# Short-lived API cache
Cache-Control: public, max-age=60, s-maxage=300
# Browser caches 60s, CDN caches 300s
```

### CloudFront Optimization
```
Origin Shield:
  - Single cache layer between edge and origin
  - Reduces origin load by 80-90%
  - Enable in region closest to origin

Compression:
  - Enable automatic compression (gzip + brotli)
  - 60-80% size reduction for text content

Cache Key:
  - Minimal: only include what varies the response
  - Don't include: random headers, cookies (unless needed)
  - Whitelist specific query params, headers
```

## 12.5 VPN

### Pritunl (Our VPN)
```hcl
# From thinkbit-devops-cloudvpn Terraform
VPC (10.0.0.0/16)
├── Public Subnet (10.0.1.0/24)
│   └── EC2 t3.medium + EIP
│       └── Pritunl VPN Server (MongoDB backend)
└── Private Subnet (10.0.2.0/24)
    └── Internal resources

# Cost: ~$35-50/month
# Security: UDP/TCP 1194 + HTTPS 443
```

### WireGuard (Alternative)
```ini
# /etc/wireguard/wg0.conf (server)
[Interface]
PrivateKey = <server-private-key>
Address = 10.200.0.1/24
ListenPort = 51820
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

[Peer]
PublicKey = <client-public-key>
AllowedIPs = 10.200.0.2/32

# systemctl enable --now wg-quick@wg0
# Advantages over OpenVPN: faster, simpler, kernel-level, less CPU
```

## 12.6 Firewall

### Security Groups (Stateful)
```yaml
# Principle: allow only what's needed, deny everything else
APISecurityGroup:
  Type: AWS::EC2::SecurityGroup
  Properties:
    GroupDescription: API servers
    VpcId: !Ref VPC
    SecurityGroupIngress:
      - Description: ALB health checks
        SourceSecurityGroupId: !Ref ALBSecurityGroup
        IpProtocol: tcp
        FromPort: 3000
        ToPort: 3000
    # Egress: defaults to allow all (restrict in high-security)

# Anti-patterns:
# - 0.0.0.0/0 on port 22 (use SSM Session Manager instead)
# - All traffic from self (be specific about ports)
# - Not using security group references (use SG-to-SG, not CIDR)
```

### NACLs (Stateless)
```
Use for: subnet-level defense-in-depth, blocking known bad IPs
Don't use for: fine-grained access control (use SGs)

Rule order matters (lowest number = highest priority):
100  ALLOW  TCP 443   0.0.0.0/0  (HTTPS in)
200  ALLOW  TCP 80    0.0.0.0/0  (HTTP in, redirect to HTTPS)
300  ALLOW  TCP 1024-65535  0.0.0.0/0  (ephemeral ports, return traffic)
*    DENY   ALL ALL   0.0.0.0/0  (default deny)
```

---

# 13. Database Administration

## 13.1 PostgreSQL

### Performance Tuning
```sql
-- Check slow queries
SELECT query, calls, total_exec_time / 1000 as total_sec,
       mean_exec_time as avg_ms, rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- Check table bloat
SELECT schemaname, tablename,
       pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as total_size,
       n_dead_tup, n_live_tup,
       round(n_dead_tup::numeric / greatest(n_live_tup, 1) * 100, 2) as dead_pct
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC;

-- Check index usage
SELECT schemaname, tablename, indexname,
       idx_scan, idx_tup_read, idx_tup_fetch,
       pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;
-- Unused indexes (idx_scan = 0): consider dropping

-- Check locks
SELECT pid, age(clock_timestamp(), query_start), usename, query, state, wait_event_type, wait_event
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY query_start;

-- Kill long-running query
SELECT pg_terminate_backend(pid);
```

### Vacuuming
```sql
-- Auto-vacuum settings (postgresql.conf)
autovacuum = on
autovacuum_vacuum_threshold = 50
autovacuum_vacuum_scale_factor = 0.1    -- vacuum when 10% rows dead
autovacuum_analyze_threshold = 50
autovacuum_analyze_scale_factor = 0.05  -- analyze when 5% rows changed

-- For large tables, lower scale factor:
ALTER TABLE large_table SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.005
);

-- Check last vacuum
SELECT schemaname, relname, last_vacuum, last_autovacuum, last_analyze
FROM pg_stat_user_tables
ORDER BY last_autovacuum NULLS FIRST;
```

### Replication
```sql
-- Streaming replication (physical, exact copy)
-- primary: postgresql.conf
wal_level = replica
max_wal_senders = 10
wal_keep_size = 1GB

-- replica: recovery.conf / standby.signal
primary_conninfo = 'host=primary port=5432 user=replication password=xxx'
hot_standby = on

-- Logical replication (selective tables, different schema ok)
-- publisher:
CREATE PUBLICATION my_pub FOR TABLE orders, users;
-- subscriber:
CREATE SUBSCRIPTION my_sub CONNECTION 'host=publisher ...' PUBLICATION my_pub;

-- Check replication lag
SELECT client_addr, state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn) as sent_lag_bytes,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) as replay_lag_bytes
FROM pg_stat_replication;
```

### Partitioning
```sql
-- Range partitioning (time-series data)
CREATE TABLE events (
    id bigserial,
    created_at timestamptz NOT NULL,
    event_type text,
    payload jsonb
) PARTITION BY RANGE (created_at);

CREATE TABLE events_2026_01 PARTITION OF events
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE events_2026_02 PARTITION OF events
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
-- Auto-create with pg_partman extension

-- Benefits: faster queries (partition pruning), easier maintenance (drop partition vs delete)
-- Caveats: unique constraints must include partition key
```

### Connection Pooling (PgBouncer)
```ini
# pgbouncer.ini
[databases]
mydb = host=rds-endpoint port=5432 dbname=mydb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction          # Best for web apps (releases conn after TX)
max_client_conn = 1000           # Max clients to pgbouncer
default_pool_size = 20           # Connections to PostgreSQL per pool
reserve_pool_size = 5            # Extra connections for burst
reserve_pool_timeout = 3

# Pool modes:
# session:     connection held for entire client session (like no pooler)
# transaction: connection released after each transaction (recommended)
# statement:   connection released after each statement (multi-statement TXs break)
```

## 13.2 SQL Server

**Our Setup:** `think-bit-rds`, SQL Server SE, `db.m6i.2xlarge`, 300GB gp3

```sql
-- TempDB optimization (for RDS, limited but check)
-- Monitor TempDB contention
SELECT * FROM sys.dm_os_wait_stats WHERE wait_type LIKE 'PAGELATCH%';

-- Index maintenance
-- Rebuild when fragmentation > 30%
ALTER INDEX idx_name ON dbo.TableName REBUILD WITH (ONLINE = ON);
-- Reorganize when fragmentation 10-30%
ALTER INDEX idx_name ON dbo.TableName REORGANIZE;

-- Check missing indexes
SELECT TOP 20
    ROUND(s.avg_total_user_cost * s.avg_user_impact * (s.user_seeks + s.user_scans), 0) AS [Impact],
    d.statement AS [Table],
    d.equality_columns, d.inequality_columns, d.included_columns
FROM sys.dm_db_missing_index_groups g
JOIN sys.dm_db_missing_index_group_stats s ON g.index_group_handle = s.group_handle
JOIN sys.dm_db_missing_index_details d ON g.index_handle = d.index_handle
ORDER BY [Impact] DESC;

-- Always On Availability Group (HA)
-- RDS: use Multi-AZ deployment (managed Always On)
-- Check replica status
SELECT ar.replica_server_name, drs.synchronization_state_desc, drs.synchronization_health_desc
FROM sys.dm_hadr_database_replica_states drs
JOIN sys.availability_replicas ar ON drs.replica_id = ar.replica_id;
```

## 13.3 Redis

```bash
# Memory analysis
redis-cli INFO memory
redis-cli --bigkeys
redis-cli MEMORY DOCTOR

# Persistence comparison:
# RDB (snapshots): faster restart, may lose recent data
# AOF (append-only): durable, larger files, slower restart
# Hybrid (RDB+AOF): recommended — RDB for fast restart, AOF for durability

# Common patterns
# Cache-aside
GET key -> miss -> query DB -> SET key value EX 300

# Write-through
SET key value -> write to DB

# Pub/Sub (real-time messaging)
SUBSCRIBE channel
PUBLISH channel message

# Sorted sets (leaderboards, rate limiting)
ZADD leaderboard 100 "player1"
ZRANGE leaderboard 0 9 WITHSCORES REV  # Top 10

# Rate limiting (sliding window)
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, now .. math.random())
  redis.call('EXPIRE', key, window)
  return 1
end
return 0
```

### Redis Cluster Mode
```bash
# Create cluster
redis-cli --cluster create \
  node1:6379 node2:6379 node3:6379 \
  node4:6379 node5:6379 node6:6379 \
  --cluster-replicas 1

# Hash tags (force related keys to same slot)
SET {user:123}:profile "data"
SET {user:123}:sessions "data"
# Both go to same slot because {user:123} hash tag matches
```

## 13.4 MongoDB

```javascript
// Replica set status
rs.status()

// Sharding
sh.enableSharding("mydb")
sh.shardCollection("mydb.orders", { userId: "hashed" })
// Shard key selection: high cardinality, even distribution, query-targeted

// Index strategies
db.orders.createIndex({ userId: 1, createdAt: -1 })  // Compound
db.orders.createIndex({ "items.productId": 1 })       // Multikey (array)
db.orders.createIndex({ location: "2dsphere" })        // Geospatial
db.orders.createIndex({ status: 1 }, { partialFilterExpression: { status: "pending" } })  // Partial

// Aggregation pipeline
db.orders.aggregate([
  { $match: { createdAt: { $gte: ISODate("2026-01-01") } } },
  { $group: {
    _id: "$userId",
    totalSpent: { $sum: "$total" },
    orderCount: { $sum: 1 },
    avgOrder: { $avg: "$total" }
  }},
  { $sort: { totalSpent: -1 } },
  { $limit: 100 }
])

// Performance: explain
db.orders.find({ userId: "123" }).explain("executionStats")
// Look for: COLLSCAN (full scan = bad), IXSCAN (index scan = good)
```

## 13.5 Migration Strategies

### Zero-Downtime Schema Migration
```
1. Expand-Contract pattern:
   Phase 1 (expand):    Add new column (nullable), deploy code that writes to BOTH
   Phase 2 (migrate):   Backfill old data to new column
   Phase 3 (contract):  Deploy code using new column only, drop old column

2. Blue/Green DB migration:
   - Create replica
   - Apply schema changes to replica
   - Test with replica
   - Promote replica (brief downtime for DNS switch)

3. Online schema migration tools:
   PostgreSQL: pg_repack (no locks), pgloader
   MySQL: gh-ost (GitHub), pt-online-schema-change (Percona)
   General: Flyway, Liquibase (versioned migrations)
```

### Migration Script Pattern
```sql
-- migrations/20260416_001_add_status_column.sql
-- +migrate Up
ALTER TABLE orders ADD COLUMN status_v2 TEXT;
CREATE INDEX CONCURRENTLY idx_orders_status_v2 ON orders(status_v2);

-- +migrate Down
DROP INDEX IF EXISTS idx_orders_status_v2;
ALTER TABLE orders DROP COLUMN IF EXISTS status_v2;

-- Key rules:
-- Every UP has a DOWN (rollback plan)
-- CONCURRENTLY for index creation (no locks)
-- Never rename columns directly (expand-contract instead)
-- Test migration on production-size data (timing matters)
```

---

# 14. Linux & Shell

## 14.1 systemd

```bash
# Service unit file: /etc/systemd/system/myapp.service
[Unit]
Description=My Application
After=network.target
Requires=network.target

[Service]
Type=notify
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/bin/server
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/myapp /var/log/myapp
PrivateTmp=true
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Resource limits
MemoryMax=1G
CPUQuota=200%
TasksMax=4096

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=myapp

[Install]
WantedBy=multi-user.target
```

```bash
# Timer (cron replacement)
# /etc/systemd/system/backup.timer
[Unit]
Description=Daily backup

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target

# systemctl enable --now backup.timer
# systemctl list-timers
```

### journald
```bash
# View logs
journalctl -u myapp.service --since "1 hour ago" --no-pager
journalctl -u myapp.service -f  # Follow
journalctl -u myapp.service -p err  # Errors only
journalctl -u myapp.service --output json-pretty  # JSON format
journalctl --disk-usage  # Check log size
journalctl --vacuum-size=500M  # Trim to 500MB
```

## 14.2 Process Management

```bash
# Signals
kill -SIGTERM $PID    # Graceful shutdown (default)
kill -SIGHUP $PID     # Reload config
kill -SIGUSR1 $PID    # App-specific (often: reopen logs)
kill -SIGKILL $PID    # Force kill (last resort, no cleanup)

# Process tree
pstree -p $PID
ps aux --forest

# Resource limits (ulimits)
ulimit -n 65535       # Max open files
ulimit -u 4096        # Max user processes
# Persistent: /etc/security/limits.conf
# myapp  soft  nofile  65535
# myapp  hard  nofile  65535

# cgroups v2 (resource isolation)
systemd-cgtop             # Top for cgroups
systemctl show myapp --property=MemoryCurrent,CPUUsageNSec
```

## 14.3 Filesystem

```bash
# LVM (Logical Volume Manager)
pvcreate /dev/xvdf                          # Create physical volume
vgcreate data-vg /dev/xvdf                  # Create volume group
lvcreate -l 100%FREE -n data-lv data-vg     # Create logical volume
mkfs.xfs /dev/data-vg/data-lv               # Format
mount /dev/data-vg/data-lv /data            # Mount

# Extend without downtime
lvextend -l +100%FREE /dev/data-vg/data-lv  # Extend LV
xfs_growfs /data                             # Extend filesystem (XFS)
# or: resize2fs /dev/data-vg/data-lv        # Extend filesystem (ext4)

# Disk usage analysis
ncdu /                  # Interactive disk usage
du -sh /var/log/*       # Directory sizes
df -h                   # Filesystem usage
lsblk                   # Block devices
```

## 14.4 Network Debugging

```bash
# Listening ports
ss -tlnp                # TCP listeners with PIDs
ss -ulnp                # UDP listeners
ss -s                   # Socket statistics summary

# Connection tracking
ss -t state established | wc -l    # Active connections
ss -t state time-wait | wc -l     # TIME_WAIT connections
conntrack -L | wc -l               # NAT connection table

# DNS debugging
dig +short api.example.com                    # A record
dig +trace api.example.com                    # Full resolution path
dig @8.8.8.8 api.example.com                 # Query specific resolver
host -t MX example.com                        # MX records

# Packet capture
tcpdump -i eth0 -n 'port 443' -c 100         # Capture 100 packets on 443
tcpdump -i eth0 -nn -A 'port 80 and host 10.0.1.5'  # HTTP traffic, ASCII
tcpdump -i eth0 -w capture.pcap               # Save to file

# Network path
mtr --report --report-cycles 10 api.example.com   # Traceroute with stats
curl -w "\nDNS: %{time_namelookup}s\nConnect: %{time_connect}s\nTLS: %{time_appconnect}s\nTTFB: %{time_starttransfer}s\nTotal: %{time_total}s\n" -o /dev/null -s https://api.example.com

# iptables / nftables
iptables -L -n -v                              # List rules with counters
iptables -A INPUT -p tcp --dport 22 -s 10.0.0.0/8 -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -j DROP
# nftables (modern replacement):
nft list ruleset
```

## 14.5 Performance Analysis

```bash
# CPU
top -bn1 | head -20           # CPU/memory overview
mpstat -P ALL 1 5             # Per-CPU stats
pidstat -u 1 5                # Per-process CPU
perf top                      # Real-time CPU profiling
perf record -g -p $PID -- sleep 30 && perf report  # Flamegraph data

# Memory
free -h                        # Memory overview
vmstat 1 10                    # Virtual memory stats
slabtop                        # Kernel slab cache
cat /proc/meminfo              # Detailed memory breakdown
cat /proc/$PID/smaps_rollup    # Per-process memory map

# Disk I/O
iostat -xz 1 5                # Disk I/O stats
iotop -ao                     # I/O by process
biosnoop                      # BPF-based I/O tracing

# System call tracing
strace -p $PID -c             # Syscall summary
strace -p $PID -e trace=network -T  # Network syscalls with time
ltrace -p $PID -c             # Library call summary

# One-liners for common diagnostics
# What's using the most memory?
ps aux --sort=-%mem | head -10
# What's using the most CPU?
ps aux --sort=-%cpu | head -10
# What files are open?
lsof -p $PID | wc -l
# What's filling the disk?
find / -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -h
```

## 14.6 Shell Scripting

### Template (Production-Grade)
```bash
#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
readonly LOG_FILE="/var/log/${SCRIPT_NAME%.sh}.log"

# Colors (only if terminal)
if [[ -t 1 ]]; then
  readonly RED='\033[0;31m'
  readonly GREEN='\033[0;32m'
  readonly YELLOW='\033[1;33m'
  readonly NC='\033[0m'
else
  readonly RED='' GREEN='' YELLOW='' NC=''
fi

log()  { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
info() { log "${GREEN}INFO${NC}  $*"; }
warn() { log "${YELLOW}WARN${NC}  $*"; }
err()  { log "${RED}ERROR${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

cleanup() {
  local exit_code=$?
  # Cleanup temp files, connections, etc.
  rm -f "${TMPFILE:-}"
  if [[ $exit_code -ne 0 ]]; then
    err "Script failed with exit code $exit_code"
  fi
  exit $exit_code
}
trap cleanup EXIT
trap 'die "Interrupted"' INT TERM

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS] <command>

Options:
  -e, --environment  Environment (staging|production)
  -d, --dry-run      Show what would be done
  -v, --verbose      Verbose output
  -h, --help         Show this help

Commands:
  deploy    Deploy the service
  rollback  Rollback to previous version
EOF
}

# Parse arguments
ENVIRONMENT=""
DRY_RUN=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
  case $1 in
    -e|--environment) ENVIRONMENT="$2"; shift 2 ;;
    -d|--dry-run)     DRY_RUN=true; shift ;;
    -v|--verbose)     VERBOSE=true; shift ;;
    -h|--help)        usage; exit 0 ;;
    --)               shift; break ;;
    -*)               die "Unknown option: $1" ;;
    *)                break ;;
  esac
done

COMMAND="${1:-}"
[[ -z "$COMMAND" ]] && die "Command required. Use --help for usage."
[[ -z "$ENVIRONMENT" ]] && die "Environment required (-e staging|production)"

# Validate
[[ "$ENVIRONMENT" =~ ^(staging|production)$ ]] || die "Invalid environment: $ENVIRONMENT"

# Retry function
retry() {
  local max_attempts="${1}"; shift
  local delay="${1}"; shift
  local attempt=1

  while true; do
    if "$@"; then
      return 0
    fi
    if [[ $attempt -ge $max_attempts ]]; then
      err "Command failed after $max_attempts attempts: $*"
      return 1
    fi
    warn "Attempt $attempt/$max_attempts failed, retrying in ${delay}s..."
    sleep "$delay"
    ((attempt++))
    delay=$((delay * 2))  # Exponential backoff
  done
}

# Require commands
require_cmd() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd"
  done
}

require_cmd aws jq curl

# Main logic
main() {
  info "Starting $COMMAND for $ENVIRONMENT"
  case "$COMMAND" in
    deploy)   deploy ;;
    rollback) rollback ;;
    *)        die "Unknown command: $COMMAND" ;;
  esac
  info "Done"
}

deploy() {
  if $DRY_RUN; then
    info "[DRY RUN] Would deploy to $ENVIRONMENT"
    return
  fi
  retry 3 5 aws ecs update-service \
    --cluster "excise-wine" \
    --service "nodejs-api-${ENVIRONMENT}" \
    --force-new-deployment \
    --region ap-southeast-7
}

rollback() {
  info "Rolling back $ENVIRONMENT..."
  # ... rollback logic
}

main
```

### Bash Idioms
```bash
# Default values
NAME="${1:-default}"
PORT="${PORT:-3000}"

# String operations
FILE="path/to/file.tar.gz"
echo "${FILE##*/}"     # file.tar.gz (basename)
echo "${FILE%.*}"      # path/to/file.tar (remove extension)
echo "${FILE%%.*}"     # path/to/file (remove all extensions)
echo "${FILE//\//-}"   # path-to-file.tar.gz (replace all)

# Arrays
declare -a SERVICES=("api" "worker" "cron")
for svc in "${SERVICES[@]}"; do echo "$svc"; done
echo "Count: ${#SERVICES[@]}"

# Associative arrays (bash 4+)
declare -A PORTS=([api]=3000 [worker]=3001 [cron]=3002)
for svc in "${!PORTS[@]}"; do echo "$svc: ${PORTS[$svc]}"; done

# Process substitution
diff <(aws s3 ls s3://bucket1/) <(aws s3 ls s3://bucket2/)

# Here-string for stdin
aws sts decode-authorization-message --encoded-message "$MSG" | jq -r '.DecodedMessage' | jq .

# Parallel execution
for svc in api worker cron; do
  deploy "$svc" &
done
wait  # Wait for all background jobs

# Lock file (prevent concurrent execution)
LOCKFILE="/var/run/${SCRIPT_NAME}.lock"
exec 200>"$LOCKFILE"
flock -n 200 || die "Another instance is already running"

# Temp file (auto-cleanup via trap)
TMPFILE="$(mktemp)"
```

---

# 15. Cost Engineering

## 15.1 AWS Cost Optimization

### Quick Wins (Immediate Savings)

| Action | Savings | Effort |
|--------|---------|--------|
| Delete unused EBS volumes | $0.08/GB/mo | Low |
| Delete unattached Elastic IPs | $3.65/mo each | Low |
| Downsize over-provisioned RDS | 20-50% | Medium |
| Switch gp2 -> gp3 | 20% on EBS | Low |
| Enable S3 Intelligent-Tiering | 20-40% on storage | Low |
| NAT Instance instead of NAT GW (dev) | ~$29/mo per AZ | Medium |
| Compute Savings Plans (1yr) | 20-30% | Low |
| Fargate Spot for non-critical | 70% | Medium |
| Reserved ElastiCache/RDS | 30-60% | Low |

### Tagging Strategy (FinOps Foundation)
```
Required tags (enforced by SCP/Config):
  Environment:  staging | production | development
  Project:      excise-wine | excise-car | platform
  Team:         devops | backend | frontend
  CostCenter:   CC-001 | CC-002
  ManagedBy:    terraform | cloudformation | manual

Optional:
  Owner:        email of resource owner
  ExpiresAt:    auto-delete date for temporary resources
  Compliance:   pci | hipaa | none
```

### Cost Explorer Queries
```bash
# Monthly cost by service
aws ce get-cost-and-usage \
  --time-period Start=2026-03-01,End=2026-04-01 \
  --granularity MONTHLY \
  --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE

# Cost by tag
aws ce get-cost-and-usage \
  --time-period Start=2026-03-01,End=2026-04-01 \
  --granularity MONTHLY \
  --metrics UnblendedCost \
  --group-by Type=TAG,Key=Project

# Budget alert
aws budgets create-budget --account-id 498952158610 --budget '{
  "BudgetName": "MonthlyTotal",
  "BudgetLimit": {"Amount": "5000", "Unit": "USD"},
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST"
}' --notifications-with-subscribers '[{
  "Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": 80},
  "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "alerts@devthinkbit.com"}]
}]'
```

## 15.2 FinOps Practices

### Showback/Chargeback Model
```
Level 1 (Showback): Teams see their costs, no billing
  -> Start here, build awareness

Level 2 (Chargeback): Teams billed for their usage
  -> Shared costs (VPC, NAT, monitoring) split by usage %
  -> Tag-based allocation (Project tag required)

Level 3 (Optimization): Teams have cost targets
  -> Cost as a non-functional requirement
  -> Architecture reviews include cost analysis
  -> FinOps team reviews top 10 spend items monthly
```

### Data Transfer Cost Traps
```
Free:
  - Inbound data transfer (internet -> AWS)
  - Same AZ, same service
  - S3 -> CloudFront (origin fetch)
  - VPC endpoints (gateway: S3, DynamoDB)

Costs money:
  - Cross-AZ: $0.01/GB each way (adds up fast!)
  - Cross-region: $0.02/GB
  - Internet egress: $0.09/GB (first 10TB)
  - NAT Gateway: $0.045/GB processed
  - VPC endpoint (interface): $0.01/GB

Optimization:
  - Keep chatty services in same AZ
  - Use VPC endpoints instead of NAT for AWS services
  - CloudFront for repeated content (cheaper than direct)
  - S3 Transfer Acceleration for cross-region uploads
  - Compress data before transfer
```

### Right-Sizing Process
```
1. Enable CloudWatch agent for memory metrics
2. Wait 2-4 weeks for baseline data
3. Use Cost Explorer right-sizing recommendations
4. Review: peak utilization < 40%? -> downsize
5. Consider burstable (t3/t4g) for variable workloads
6. Consider Graviton for consistent 20% savings
7. Apply in staging first, monitor 1 week, then production
```

## 15.3 Multi-Cloud Cost Comparison

### Compute (approximate, on-demand, ap-southeast-1)

| Spec | AWS (m7g.large) | GCP (e2-standard-2) | Azure (B2s) |
|------|-----------------|---------------------|-------------|
| vCPU | 2 | 2 | 2 |
| RAM | 8 GB | 8 GB | 4 GB |
| Cost/hr | $0.0816 | $0.0670 | $0.0416 |
| Cost/mo | ~$60 | ~$49 | ~$30 |
| Notes | Graviton ARM | x86 | Burstable |

### Managed PostgreSQL (approximate, 2 vCPU, 8GB RAM, 100GB)

| AWS RDS | GCP Cloud SQL | Azure Database |
|---------|---------------|---------------|
| ~$150/mo | ~$130/mo | ~$120/mo |
| Multi-AZ +100% | HA +100% | Zone redundant included |

### Object Storage (per GB/mo)

| Tier | AWS S3 | GCP GCS | Azure Blob |
|------|--------|---------|-----------|
| Standard | $0.023 | $0.020 | $0.018 |
| Infrequent | $0.0125 | $0.010 | $0.010 |
| Archive | $0.004 | $0.004 | $0.002 |
| Egress/GB | $0.09 | $0.12 | $0.087 |

---

# Appendix: Quick Reference Commands

## AWS CLI Essentials
```bash
# Identity
aws sts get-caller-identity
aws sts decode-authorization-message --encoded-message $MSG | jq -r '.DecodedMessage' | jq .

# EC2
aws ec2 describe-instances --filters "Name=tag:Environment,Values=staging" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,PrivateIpAddress]' --output table

# ECS
aws ecs list-services --cluster excise-wine --region ap-southeast-7
aws ecs describe-services --cluster excise-wine --services nodejs-api --region ap-southeast-7
aws ecs update-service --cluster excise-wine --service nodejs-api --force-new-deployment --region ap-southeast-7
aws ecs list-tasks --cluster excise-wine --service-name nodejs-api --region ap-southeast-7
aws logs tail /ecs/excise-wine-nodejs-api-staging --follow --region ap-southeast-7

# Lambda
aws lambda invoke --function-name excise-wine-authen-staging --payload '{}' /dev/stdout --region ap-southeast-7
aws lambda list-functions --region ap-southeast-7 --query 'Functions[].[FunctionName,Runtime,MemorySize]' --output table
aws lambda update-function-code --function-name myFunc --s3-bucket bucket --s3-key key --region ap-southeast-7

# S3
aws s3 ls s3://thinkbit-devops-artifacts/ --recursive --summarize
aws s3 sync s3://source s3://dest --source-region ap-southeast-1 --region ap-southeast-7

# CloudFormation
aws cloudformation describe-stacks --region ap-southeast-7 --query 'Stacks[].[StackName,StackStatus]' --output table
aws cloudformation describe-stack-events --stack-name mystack --region ap-southeast-7 --query 'StackEvents[].[Timestamp,ResourceType,LogicalResourceId,ResourceStatus,ResourceStatusReason]' --output table

# CloudWatch
aws logs get-log-events --log-group-name /ecs/myservice --log-stream-name ecs/app/taskid --limit 50 --region ap-southeast-7
aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name CPUUtilization \
  --dimensions Name=ServiceName,Value=nodejs-api Name=ClusterName,Value=excise-wine \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%S) --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 --statistics Average --region ap-southeast-7

# Secrets Manager
aws secretsmanager get-secret-value --secret-id excise/wine/staging/db --region ap-southeast-7 | jq -r '.SecretString'

# SSM (Parameter Store)
aws ssm get-parameter --name /excise-wine-nodejs-api-staging/build-number --region ap-southeast-1
aws ssm put-parameter --name /key --value "val" --type SecureString --overwrite --region ap-southeast-1

# CodeBuild
aws codebuild list-projects --region ap-southeast-1
aws codebuild start-build --project-name excise-wine-nodejs-api-staging-build --region ap-southeast-1
aws codebuild batch-get-builds --ids <build-id> --region ap-southeast-1
```

## Kubernetes Essentials
```bash
# Context
kubectl config get-contexts
kubectl config use-context my-cluster

# Debugging
kubectl get pods -n production -o wide
kubectl describe pod <pod> -n production
kubectl logs <pod> -n production --tail=100 -f
kubectl logs <pod> -n production -c <container> --previous  # Previous crash
kubectl exec -it <pod> -n production -- /bin/sh
kubectl top pods -n production --sort-by=memory

# Rollout
kubectl rollout status deployment/api -n production
kubectl rollout history deployment/api -n production
kubectl rollout undo deployment/api -n production --to-revision=3
kubectl rollout restart deployment/api -n production

# Quick debug pod
kubectl run debug --image=busybox -it --rm -- /bin/sh
kubectl run debug --image=nicolaka/netshoot -it --rm -- /bin/bash  # Network debugging

# Resources
kubectl get all -n production
kubectl get events -n production --sort-by='.lastTimestamp'
kubectl api-resources  # All resource types
```

## Terraform Essentials
```bash
terraform init
terraform workspace list
terraform workspace select staging
terraform plan -var-file=environments/staging/terraform.tfvars -out=plan.tfplan
terraform apply plan.tfplan
terraform import 'aws_instance.web' i-1234567890abcdef0
terraform state list
terraform state show 'aws_instance.web'
terraform output -json
terraform graph | dot -Tsvg > graph.svg
terraform force-unlock <lock-id>
```

## Docker Essentials
```bash
# Build
docker build -t myapp:latest --target production .
docker buildx build --platform linux/amd64,linux/arm64 -t myapp:latest --push .

# Debug
docker exec -it <container> /bin/sh
docker logs <container> --tail=100 -f
docker stats
docker system df  # Disk usage
docker system prune -a --volumes  # Clean everything

# Compose
docker compose up -d
docker compose logs -f api
docker compose exec api /bin/sh
docker compose down -v  # Remove volumes too
```

---

---

# 16. Project-Specific Knowledge (MUST READ)

These files contain our ACTUAL infrastructure, repos, and services. This ops-skills.md has general patterns — the files below have our specific configs, stacks, and repos. **Always cross-reference.**

## Architecture & Teams
- **`knowledge/architecture.md`** — Agent team roles (orchestrator, dev, ops, architect, qa, reviewer), when to use swarm vs direct, decision framework, code review checklist

## Infrastructure as Code (Our Configs)
- **`knowledge/terraform.md`** — All Terraform projects:
  - `thinkbit-devops-iac`: AWS (RDS, EC2, VPC, Lambda, RDS Proxy) — S3 backend `thinkbit-terraform-state`, workspaces dev/staging/prod
  - `gcp-terraform`: GCP (5,170-line main.tf, VMs, Cloud SQL, Storage)
  - `azure-terraform`: MFA Portal + AI Portal (App Service, Azure SQL, KeyVault)
  - `thinkbit-devops-cloudvpn`: Pritunl VPN (EC2 t3.medium, ~$35-50/mo)
  - `thinkbit-devops-SwithRole`: Multi-account IAM (Organizations, SCPs, CloudTrail)
  - `AWS-Fix-Assassment`: Security remediation (5,759 findings, 1,964 FAIL, ~67% complete)

## CloudFormation (All Stacks)
- **`knowledge/cloudformation.md`** — Complete CF stack guide:
  - Stack naming: `{org}-{project}-{module}[-{environment}]`
  - Creation order: State -> ECR -> Build -> Deploy -> Service (5 steps)
  - 30+ stacks in apse1, 24+ in apse7
  - S3 artifact structure (legacy `devops/template/` + new `thinkbit/{stack-name}/{version}/`)
  - Sceptre production orchestration via Jenkins
  - DevSecOps scan matrix (gitleaks, semgrep, cfn-lint, SCA)

## DevOps Repos (All 30+)
- **`knowledge/devops-repos.md`** — Every repo in `~/develope/DevOps/`:
  - CI/CD: thinkbit-devops-material (CF templates), thinkbit-devops-modules (per-service params), thinkbit-devops-modules-prod (Sceptre), thinkbit-devops-pipeline (GitHub Actions)
  - IaC: thinkbit-devops-iac, gcp-terraform, azure-terraform
  - Security: thinkbit-devops-prowler (monthly scans), AWS-Fix-Assassment, thinkbit-devops-sonarqube
  - CI/CD platforms: thinkbit-devops-jenkins
  - Data: backup (RDS snapshots), bucket-sync (S3<->GCS), Cost (inventory)
  - Testing: wine-loadtest-k6 (50k users)
  - Utilities: credential (SSH/SSL), script (AWS utilities)

## Axentx Projects (Platform & AI)
- **`knowledge/axentx-projects.md`** — All axentx projects:
  - **Costinel**: Cloud Cost Governance (React/Supabase/PostgreSQL/Kong/Redis)
  - **Vanguard**: Cloud Security Platform (FastAPI/Neo4j/PostgreSQL/Redis/Celery/DefectDojo/Wazuh)
  - **AxiomOps**: AI DevOps Decision Platform (Turbo monorepo, 9 services, LGTM stack, OPA, Qdrant, OpenAI+Claude)
  - **Arkship**: Infrastructure Automation + AI (FastAPI/Temporal/K8s/Neo4j/Qdrant, 12 workflow presets, 6 AI roles)
  - **Surrogate-1**: Custom LLM fine-tuning (GLM-5/Huawei Ascend 910B, QLoRA, ~$30-60/mo)
  - **Workio**: HR Time Tracking via LINE OA (React/Express/PostgreSQL)

## Excise Services (Our Main Product)
- **`knowledge/excise-services.md`** — All service internals:
  - wine-nodejs-api: Express 5.1, MSSQL/Sequelize, port 3000, API v2-v6
  - wine-go-api: Go 1.24, Gorilla Mux, Firestore, port 8080
  - wine-authen: Lambda, Firebase->Cognito migration
  - wine-proxy: Nginx, SSL/TLS, upstream routing to winefasttrack.excise.go.th
  - car-backend: Express 4.21, Prisma/MSSQL, Zod validation
  - car-cron: Lambda, Thai Customs currency scraping

## Workspace Map
- **`knowledge/workspace-map.md`** — Quick lookup for EVERY project path, cloud summary, all repos

---

## Related Patterns (Graph Links)

- [[../patterns/auth/401-empty-api-key]] · [[../patterns/auth/mcp-token-missing]]
- [[../patterns/engineering/cost-conscious]] · [[../patterns/engineering/right-sized-no-overengineer]] · [[../patterns/engineering/codebase-first]]
- [[../patterns/security/third-party-audit-workflow]] · [[../patterns/security/ignore-malware-reminders]]
- [[../patterns/skills/auto-activation]]
- [[../patterns/MOC|🧭 Knowledge Graph Hub]]

> This file is loaded into every AI session as the universal ops reference.
> Cross-reference with project-specific knowledge files above for our actual infrastructure.
> Last updated: 2026-04-16
> Location: ~/Documents/Obsidian Vault/AI-Hub/knowledge/ops-skills.md
