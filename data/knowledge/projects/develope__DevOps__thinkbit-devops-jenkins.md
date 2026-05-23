---
name: thinkbit-devops-jenkins
path: /Users/Ashira/develope/DevOps/thinkbit-devops-jenkins
tags: ["project", "codebase", "docker"]
last_indexed: 2026-05-01
type: project
---

# thinkbit-devops-jenkins

**Path**: `/Users/Ashira/develope/DevOps/thinkbit-devops-jenkins`
**Group**: DevOps
**Languages**: unknown
**Frameworks**: Docker
**LOC**: ~0
**Deps**: 0

## README
thinkbit devops jenkins - CI/CD managed repository

## Git
- Branch: `main`
- Last commit: 2026-04-13 12:29:34 +0700 Add Route53 auto-DNS registration on ASG instance launch
- Commits (last 30d): 3

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📄 README.md
📄 appspec.yml
📄 docker-compose.yaml
📁 jenkins
  📁 init.groovy.d
    📄 01-seed-jobs.groovy
📁 pipelines
  📁 devops-release
    📄 Jenkinsfile.sceptre-cd
  📁 excise-wine
    📄 Jenkinsfile.android-staging
    📄 Jenkinsfile.ios-staging
  📁 qa-classiccar
    📄 Jenkinsfile.automate-test
  📁 qa-wine
    📄 Jenkinsfile.automate-test
📁 scripts
  📄 after_install.sh
  📄 before_install.sh
  📄 start_application.sh
  📄 stop_application.sh
  📄 validate_service.sh
📁 template
  📄 thinkbit-devops-jenkins-buildspec.yaml
  📄 thinkbit-devops-jenkins-template.yaml
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
