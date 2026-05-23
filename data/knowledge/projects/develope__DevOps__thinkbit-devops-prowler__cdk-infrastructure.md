---
name: cdk-infrastructure
path: /Users/Ashira/develope/DevOps/thinkbit-devops-prowler/cdk-infrastructure
tags: ["project", "codebase", "javascript-typescript", "typescript"]
last_indexed: 2026-05-01
type: project
---

# cdk-infrastructure

**Path**: `/Users/Ashira/develope/DevOps/thinkbit-devops-prowler/cdk-infrastructure`
**Group**: thinkbit-devops-prowler
**Languages**: JavaScript/TypeScript
**Frameworks**: TypeScript
**LOC**: ~7,377
**Deps**: 9

## README
AWS CDK infrastructure for automated security monitoring using Prowler, with VPN access via Pritunl and policy enforcement via Cloud Custodian. - **Region**: ap-southeast-7 (Jakarta) - **Scan Regions**: ap-southeast-7, ap-southeast-1 - **Compute**: Single EC2 t3.medium instance - **Storage**: S3 bucket for Prowler reports - **Notifications**: SNS email notifications - **VPN**: Pritunl VPN server for AWS network access

## Git
- Branch: `main`
- Last commit: 2026-04-13 12:29:42 +0700 Change Prowler scan schedule from daily to biweekly
- Commits (last 30d): 1

## Key dependencies
- `@types/jest`
- `@types/node`
- `aws-cdk`
- `aws-cdk-lib`
- `constructs`
- `jest`
- `ts-jest`
- `ts-node`
- `typescript`

## Scripts
- `build`
- `watch`
- `test`
- `cdk`

## Structure
```
📄 AGENTS.md
📄 README.md
📄 cdk.context.json
📄 cdk.json
📁 cdk.out
  📄 ProwlerVpnMonitoringStack.assets.json
  📄 ProwlerVpnMonitoringStack.metadata.json
  📄 ProwlerVpnMonitoringStack.template.json
  📁 asset.7fa1e366ee8a9ded01fc355f704cff92bfd179574e6f9cfee800a3541df1b200
    📄 __entrypoint__.js
    📄 index.js
  📄 cdk.out
  📄 manifest.json
  📄 tree.json
📁 custodian-policies
  📄 deploy-custodian.sh
  📄 iam-remediation.yml
  📄 s3-remediation.yml
  📄 setup-custodian-notifications.sh
  📄 sg-remediation.yml
📄 deploy.sh
📄 jest.config.js
📁 lib
  📄 cdk-infrastructure-stack.d.ts
  📄 cdk-infrastructure-stack.js
  📄 cdk-infrastructure-stack.ts
  📄 prowler-vpn-monitoring-stack.d.ts
  📄 prowler-vpn-monitoring-stack.js
  📄 prowler-vpn-monitoring-stack.ts
📄 package-lock.json
📄 package.json
📁 test
  📄 cdk-infrastructure.test.d.ts
  📄 cdk-infrastructure.test.js
  📄 cdk-infrastructure.test.ts
📄 tsconfig.json
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
