---
name: excise-car-backend
path: /Users/Ashira/develope/Excise/Car/excise-car-backend
tags: ["project", "codebase", "javascript-typescript", "express", "typescript", "firebase", "aws-sdk", "prisma"]
last_indexed: 2026-05-01
type: project
---

# excise-car-backend

**Path**: `/Users/Ashira/develope/Excise/Car/excise-car-backend`
**Group**: Car
**Languages**: JavaScript/TypeScript
**Frameworks**: Express, TypeScript, Firebase, AWS SDK, Prisma, Sequelize, MSSQL, Docker
**LOC**: ~125,561
**Deps**: 40

## README
> This guide will help you set up the development environment, install dependencies, and run the API server locally. - [Links](#-links) - [Introduction](#-introduction) - [Requirements](#-requirements) - [Installation](#-installation) - [Development](#-development) - [Deployment](#-deployment) 1. [Site Develop](http://localhost:5173/) 2. [Site Staging](https://classiccar-excise.devthinkbit.com/) 3. [Site Product](https://classiccars.excise.go.th/)

## Git
- Branch: `staging`
- Last commit: 2026-04-20 10:24:27 +0700 feat(vpc): add SNS Interface Endpoint in single AZ
- Commits (last 30d): 33

## Key dependencies
- `@aws-sdk/client-s3`
- `@aws-sdk/s3-request-presigner`
- `@prisma/client`
- `@types/express`
- `@types/jest`
- `@types/mssql`
- `@types/node-fetch`
- `@types/supertest`
- `@types/swagger-jsdoc`
- `@types/swagger-ui-express`
- `copyfiles`
- `cors`
- `dayjs`
- `dotenv`
- `express`

## Scripts
- `start`
- `start:docker`
- `set-alias`
- `set-alias:dev`
- `set-alias:prod`
- `test`
- `build`
- `watch`
- `dev`
- `deploy`

## Structure
```
📄 Dockerfile
📄 Procfile
📄 README.Docker.md
📁 __tests__
  📁 v2
    📁 dashboard
📄 compose.yaml
📁 cron
  📄 package-lock.json
  📄 package.json
  📁 prisma
    📄 schema.prisma
  📄 prisma.config.ts
  📁 src
    📄 develop.ts
    📁 handlers
    📄 index.ts
    📁 providers
    📁 utils
  📄 tsconfig.json
  📄 webpack.config.js
📄 docker-entrypoint.sh
📁 docs
  📄 LOGGING.md
📄 ecosystem.config.js
📄 exciseclassiccar-firebase-adminsdk-gx30v-10145e6472.json
📁 generated
  📁 prisma
    📄 default.d.ts
    📄 default.js
    📄 edge.d.ts
    📄 edge.js
    📄 index-browser.js
    📄 index.d.ts
    📄 index.js
    📄 libquery_engine-darwin-arm64.dylib.node
    📄 package.json
    📁 runtime
    📄 schema.prisma
    📄 wasm.d.ts
    📄 wasm.js
📄 jest.config.ts
📄 nodejsapp.conf
📄 nodemon.json
📄 package-lock.json
📄 package.json
📁 prisma
  📁 migrations
    📁 20260408_add_body_style
    📄 migration_lock.toml
  📄 schema.prisma
📄 readme.md
📁 scripts
  📄 _deploy.sh
  📄 connect.sh
  📄 deploy-quick.sh
  📄 deploy.sh
  📄 discord.sh
  📄 migrate-body-styles.ts
  📄 set-alias.js
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
