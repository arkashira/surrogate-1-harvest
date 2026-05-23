---
name: excise-wine-api
path: /Users/Ashira/develope/Excise/Wine/excise-wine-api
tags: ["project", "codebase", "javascript-typescript", "express", "typescript", "firebase", "aws-sdk", "sequelize"]
last_indexed: 2026-05-01
type: project
---

# excise-wine-api

**Path**: `/Users/Ashira/develope/Excise/Wine/excise-wine-api`
**Group**: Wine
**Languages**: JavaScript/TypeScript
**Frameworks**: Express, TypeScript, Firebase, AWS SDK, Sequelize, MSSQL, Docker
**LOC**: ~17,299
**Deps**: 36

## README
> API : Excise Wine (Wine1&Wine2) > กรมสรรพสามิต - Excise Department - [Requirement](#📝-requirement) - [Relate Tech stacks](#📦-relate-tech-stacks) - [Setting up!](#⚙️-setting-up) - [Development Server](#🖥️-development-server) - [Deployment](#✈️-deploy) |__Tools__|__Version__|__Required__|__Description__| |--|--|--|--| |[NodeJs](https://nodejs.org/en)|v22.14.0|✅|Node runtime| |[NVM](https://github.com/nvm-sh/nvm)|-||Node version manager|

## Git
- Branch: `release/aws`
- Last commit: 2025-12-11 16:07:04 +0700 feat: Add new Elastic Search Python sub-project with initial setup and dependencies.
- Commits (last 30d): 0

## Key dependencies
- `@aws-sdk/client-s3`
- `@aws-sdk/s3-request-presigner`
- `@faker-js/faker`
- `@google-cloud/vertexai`
- `@types/cors`
- `@types/crypto-js`
- `@types/express`
- `@types/formidable`
- `@types/mssql`
- `@types/node`
- `@types/node-fetch`
- `@types/nodemailer`
- `ajv`
- `ajv-errors`
- `algoliasearch`

## Scripts
- `start`
- `set-alias:dev`
- `set-alias:prod`
- `build`
- `dev`

## Structure
```
📄 Dockerfile
📄 compose.yaml
📄 nodemon.json
📄 package-lock.json
📄 package.json
📁 public
  📄 hello-world.txt
📄 readme.md
📁 scripts
  📄 set-alias.js
📁 src
  📁 app
    📁 config
    📁 controller
    📁 middleware
    📁 request
    📁 reusable
    📁 routes
  📄 index.ts
  📁 provider
    📄 logger.ts
    📄 storage.ts
  📁 utils
    📄 data.ts
    📄 storage.ts
📁 sub-projects
  📁 elastic-search
    📄 README.md
    📄 main.py
    📄 pyproject.toml
    📄 uv.lock
📄 tbit-excise-poc-913a0-firebase-adminsdk-fbsvc-db82e3ef0a.json
📁 template
  📄 excise-wine-node-api-params.json
  📄 excise-wine-node-api-template.yaml
📄 tsconfig.json
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
