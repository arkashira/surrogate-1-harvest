---
name: excise-wine-python-api
path: /Users/Ashira/develope/Excise/Wine/excise-wine-python-api
tags: ["project", "codebase", "python", "docker"]
last_indexed: 2026-05-01
type: project
---

# excise-wine-python-api

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-python-api`
**Group**: Wine
**Languages**: Python
**Frameworks**: Docker
**LOC**: ~180
**Deps**: 0

## README
excise wine python-api - CI/CD managed repository ```bash ./bin/load-script ```

## Git
- Branch: `staging/aws`
- Last commit: 2025-12-18 18:51:17 +0700 fix: remove Version from params.json before deploy
- Commits (last 30d): 0

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📁 Dockerfiles
  📁 multi-stage
    📄 Dockerfile
  📁 multistage-with-compile-c
    📄 Dockerfile
  📁 single-stage
    📄 Dockerfile
📄 README.Docker.md
📄 README.md
📄 buildspec.yaml
📄 compose.yaml
📄 excise-wine-python-api-prod-buildspec.yaml
📄 main.py
📄 pyproject.toml
📁 server
  📄 app.py
  📁 services
    📄 algolia.py
    📄 customs_redis.py
📁 template
  📄 excise-wine-python-api-prod-template.yaml
  📄 excise-wine-python-api-staging-buildspec.yaml
  📄 excise-wine-python-api-staging-template.yaml
📄 uv.lock
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
