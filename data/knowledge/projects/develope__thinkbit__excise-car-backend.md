---
name: excise-car-backend
path: /Users/Ashira/develope/thinkbit/excise-car-backend
tags: ["project", "codebase", "javascript-typescript", "express", "typescript", "firebase", "aws-sdk", "prisma"]
last_indexed: 2026-05-01
type: project
---

# excise-car-backend

**Path**: `/Users/Ashira/develope/thinkbit/excise-car-backend`
**Group**: thinkbit
**Languages**: JavaScript/TypeScript
**Frameworks**: Express, TypeScript, Firebase, AWS SDK, Prisma, Sequelize, MSSQL, Docker
**LOC**: ~18,876
**Deps**: 40

## README
(no README found)

## Git
- Branch: `main`
- Last commit: 2026-01-22 17:33:32 +0700 chore: Configure PM2 to run compiled `dist` files directly, update module aliases to `dist`, and set `NODE_ENV` for the start script.
- Commits (last 30d): 0

## Key dependencies
- `@aws-sdk/client-s3`
- `@aws-sdk/s3-request-presigner`
- `@prisma/adapter-mssql`
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

## Scripts
- `start`
- `set-alias`
- `set-alias:dev`
- `set-alias:prod`
- `test`
- `build`
- `watch`
- `dev`
- `deploy`
- `db:push`

## Structure
```
📄 Dockerfile
📄 Procfile
📄 README.Docker.md
📁 __tests__
  📄 setup.ts
  📁 v2
    📁 dashboard
📄 compose.yaml
📄 ecosystem.config.js
📄 enwfile.md
📄 jest.config.ts
📄 nodejsapp.conf
📄 nodemon.json
📄 package-lock.json
📄 package.json
📁 prisma
  📄 schema.prisma
📄 prisma.config.ts
📁 scripts
  📄 connect.sh
  📄 deploy-quick.sh
  📄 deploy.sh
  📄 discord.sh
  📄 set-alias.js
📁 src
  📁 app
    📁 exceptions
    📁 middleware
    📁 models
    📁 providers
    📁 routes
    📁 validation
  📄 app.ts
  📁 bootstrap
    📁 firebase
    📁 firebase-client
    📄 index.ts
    📄 sequelize.ts
  📁 config
    📄 env.ts
    📄 links.ts
    📄 officer-view-permission.ts
    📄 regex.ts
  📁 dict
    📄 auth-mode.ts
    📄 cart.ts
    📄 enum-group.ts
    📄 excise-location-code.ts
    📄 import.ts
    📄 notification.ts
    📄 officer-level.ts
    📄 user-type.ts
  📁 docs
    📁 components
    📄 devzone.yml
    📁 gwws
  📄 index.ts
  📁 types
    📄 app.ts
    📄 db.ts
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
