---
name: excise-wine-go-api
path: /Users/Ashira/develope/Excise/Wine/excise-wine-go-api
tags: ["project", "codebase", "docker"]
last_indexed: 2026-05-01
type: project
---

# excise-wine-go-api

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-go-api`
**Group**: Wine
**Languages**: unknown
**Frameworks**: Docker
**LOC**: ~500,009
**Deps**: 0

## README
The donkey project is api a service for fultter applications on Excise - google cloud functions (terminate) - MSSQL - GoLang/Mux - [Authication](/docs/auth.md) - [Master](/docs/master.md) - [User](/docs/user.md) - [Wine](/docs/wine.md) - [Accesslog](/docs/accesslog.md) - [Dashboard](/docs/dashboard.md) - [Notification](/docs/notification.md) - [User Group](/docs/usergroup.md) - [System](/docs/system.md) - [Report](/docs/report.md)

## Git
- Branch: `staging/aws`
- Last commit: 2026-04-22 16:53:44 +0700 add bottleSize handle to lite
- Commits (last 30d): 28

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📄 LICENSE
📄 README.Docker.md
📄 README.md
📁 api
  📄 README.md
  📁 common
    📁 config
    📄 go.mod
    📄 go.sum
    📄 main.go
    📁 models
    📁 repositorys
    📁 services
  📁 functions
    📁 accesslog
    📁 auth
    📁 dashboard
    📁 develop
    📁 master
    📁 notification
    📁 ocr
    📁 report
    📁 system
    📁 user
    📁 usergroup
    📁 wine
📁 assets
  📄 README.md
  📄 key.json
📁 cmd
  📄 README.md
  📁 local
    📄 go.mod
    📄 go.sum
    📄 local
    📄 main.go
    📄 server
    📁 vendor
📄 compose.yaml
📁 docs
  📄 accesslog.md
  📄 auth.md
  📄 dashboard.md
  📄 deploy.md
  📄 infrastructure-status.md
  📄 master.md
  📄 notification.md
  📄 ocr.md
  📄 report.md
  📄 system.md
  📄 user.md
  📄 usergroup.md
  📄 wine.md
📁 env
  📄 client.sh
  📄 develop.yml
  📄 poc.yml
  📄 pocdn.yml
  📄 release.yml
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
