---
name: alfresco
path: /Users/Ashira/develope/DevOps/MFA/alfresco
tags: ["project", "codebase"]
last_indexed: 2026-05-01
type: project
---

# alfresco

**Path**: `/Users/Ashira/develope/DevOps/MFA/alfresco`
**Group**: MFA
**Languages**: unknown
**Frameworks**: none detected
**LOC**: ~123
**Deps**: 0

## README
Production-ready Kubernetes deployment for Alfresco Content Services Community Edition using Helm charts with optimized configurations for staging and production environments. - 🎯 **Separate Environments**: Staging and Production with distinct configurations - 🔧 **Highly Customizable**: Component-specific configurations - 🚀 **Performance Optimized**: JVM, Database, and Solr tuning - 🔒 **Security Hardened**: Network Policies, Pod Security, Sealed Secrets

## Git
- Branch: `?`
- Last commit: ?
- Commits (last 30d): ?

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 DEPLOYMENT-STATUS.md
📄 Makefile
📄 README.md
📄 SUMMARY.md
📁 base
  📁 charts
    📁 alfresco-content-services-community-3.0.1
  📄 configmap-repository.yaml
  📄 configmap-search.yaml
  📄 configmap-share.yaml
  📄 configmap-transform.yaml
  📄 kustomization.yaml
  📄 namespace.yaml
  📄 networkpolicy.yaml
  📄 poddisruptionbudget.yaml
  📄 pvc.yaml
  📄 secret-sealed.yaml
  📄 servicemonitor.yaml
  📄 values.yaml
📄 docker-compose-alfresco.yml
📁 k8s-alfresco
  📄 activemq-deployment.yaml
  📄 activemq-service.yaml
  📄 alfresco-deployment.yaml
  📄 alfresco-service.yaml
  📄 alfresco-staging-namespace.yaml
  📄 postgres-data-persistentvolumeclaim.yaml
  📄 postgres-deployment.yaml
  📄 postgres-service.yaml
  📄 share-deployment.yaml
  📄 share-service.yaml
  📄 solr6-deployment.yaml
  📄 solr6-service.yaml
📄 kustomization.yaml
📁 overlays
  📁 production
    📄 backup-cronjob.yaml
    📄 hpa-custom.yaml
    📄 kustomization.yaml
    📄 namespace-production.yaml
    📄 pvc-production.yaml
    📄 secrets-production.yaml
    📄 secrets.env
    📄 values-production.yaml
    📄 vpa.yaml
  📁 staging
    📁 charts
    📄 kustomization.yaml
    📄 namespace-staging.yaml
    📄 values-staging-simple.yaml
    📄 values-staging.yaml
📁 scripts
  📄 api-version-converter.py
  📄 api-version-converter.sh
  📄 deploy-production.sh
  📄 deploy-staging.sh
  📄 fix-k8s-manifests.py
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
