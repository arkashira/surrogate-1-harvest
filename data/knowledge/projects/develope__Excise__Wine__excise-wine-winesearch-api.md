---
name: excise-wine-winesearch-api
path: /Users/Ashira/develope/Excise/Wine/excise-wine-winesearch-api
tags: ["project", "codebase", "python", "docker"]
last_indexed: 2026-05-01
type: project
---

# excise-wine-winesearch-api

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-winesearch-api`
**Group**: Wine
**Languages**: Python
**Frameworks**: Docker
**LOC**: ~2,718
**Deps**: 0

## README
FastAPI service that identifies wine from images (via Google Lens) and retrieves merchant pricing (via Google AI Mode). Includes a built-in web UI at `/`. - **Runtime:** Python 3.11, FastAPI, Uvicorn - **Image Recognition:** SerpAPI Google Lens - **Price Search:** SerpAPI Google AI Mode - **Image Hosting:** ImgBB (temporary upload, 5-min auto-delete) - **Auth:** JWT (HS256) for external POST requests - **Deploy:** Docker → AWS ECR → ECS (CodeBuild CI/CD)

## Git
- Branch: `staging`
- Last commit: 2026-04-27 09:21:33 +0700 Merge main: pull Dockerfile and docker-compose updates
- Commits (last 30d): 28

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📄 README.md
📄 api_image_search.py
📁 api_js_version
  📄 Dockerfile.js
  📄 api_image_search.js
  📄 package.json
📄 docker-compose.yml
📁 public
  📄 index.html
📄 requirements.txt
📁 template
  📄 excise-wine-winesearch-api-staging-buildspec.yaml
  📄 excise-wine-winesearch-api-staging-template.yaml
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
